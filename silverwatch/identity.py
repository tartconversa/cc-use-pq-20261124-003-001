"""经营身份变化的登记与解析。

旧的服务方编号不会被改写：更名、承接营业或迁址都以独立的身份变更记录表达，
因此旧公告、旧触达批次和旧线索仍按事件发生时的主体解释。
只有明确确认且已到生效时间的变更才进入当前视图；未确认的相似名称不会自动合并。
当前供给计数按接续链的末端主体去重，避免新旧编号重复计量同一份容量。
"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlite3 import Connection

from silverwatch.contracts import ProviderIdentityChange
from silverwatch.errors import conflict, invalid, missing
from silverwatch.ledger import append, current_entities

EVENT_TYPE = "provider_identity"
IMMUTABLE_FIELDS = ("predecessor_id", "successor_id", "change_type", "effective_from", "scope")
CONFIRMATION_FIELDS = ("confirmed_by", "confirmed_at", "evidence_ref")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _instant(value: datetime | None) -> datetime:
    instant = value or now()
    if instant.tzinfo is None:
        raise invalid("identity_time", "身份解析时点必须包含时区")
    return instant.astimezone(timezone.utc)


def _business_time(at: datetime | None, as_of: datetime | None) -> datetime:
    """业务时点缺省时沿用观察截点，避免用今天的身份解释历史观察。"""
    if at is not None:
        return _instant(at)
    if as_of is not None:
        return _instant(as_of)
    return now()


def _current_change(db: Connection, change_id: str) -> dict | None:
    return next((row for row in current_entities(db, EVENT_TYPE)
                 if row["entity_id"] == change_id), None)


def _confirmed_records(db: Connection, *, cut: int | None = None,
                       as_of: datetime | None = None) -> list[dict]:
    return [row for row in current_entities(db, EVENT_TYPE, cut=cut, as_of=as_of)
            if row["payload"]["status"] == "confirmed"]


def _detect_cycle(edges: dict[str, str]) -> bool:
    for start in edges:
        seen = {start}
        node = edges[start]
        while node in edges:
            if node in seen:
                return True
            seen.add(node)
            node = edges[node]
    return False


def _validate_confirmation(db: Connection, change: ProviderIdentityChange) -> None:
    records = [row for row in _confirmed_records(db) if row["entity_id"] != change.change_id]
    for scope in change.scope:
        edges: dict[str, str] = {}
        for row in records:
            value = row["payload"]
            if scope not in value["scope"]:
                continue
            prior = edges.get(value["predecessor_id"])
            if prior is not None and prior != value["successor_id"]:
                raise conflict("identity_conflict", "该服务方在这一用途下已有另一项已确认的承接关系",
                               {"predecessor_id": value["predecessor_id"], "scope": scope})
            edges[value["predecessor_id"]] = value["successor_id"]
        if change.predecessor_id in edges:
            raise conflict("identity_duplicate", "该服务方在这一用途下的承接关系已经确认",
                           {"predecessor_id": change.predecessor_id, "scope": scope})
        edges[change.predecessor_id] = change.successor_id
        if _detect_cycle(edges):
            raise conflict("identity_cycle", "经营身份变更不能形成循环",
                           {"predecessor_id": change.predecessor_id,
                            "successor_id": change.successor_id, "scope": scope})


def register(db: Connection, change: ProviderIdentityChange | dict, source_ref: str,
             revision: int) -> dict:
    if not isinstance(change, ProviderIdentityChange):
        change = ProviderIdentityChange.model_validate(change)
    known = {row["entity_id"] for row in current_entities(db, "provider")}
    for provider_id in (change.predecessor_id, change.successor_id):
        if provider_id not in known:
            raise missing("provider", provider_id)
    body = change.model_dump(mode="json")
    prior = _current_change(db, change.change_id)
    if prior is not None:
        old = prior["payload"]
        if old == body:
            return {"seq": prior["seq"], "digest": prior["digest"], "replayed": True}
        for field in IMMUTABLE_FIELDS:
            if old[field] != body[field]:
                raise conflict("identity_identity", "经营身份变更的主体与生效范围不能改写",
                               {"change_id": change.change_id, "field": field})
        if old["status"] == "confirmed":
            raise conflict("identity_sealed", "已确认的经营身份变更不能再改写",
                           {"change_id": change.change_id})
        if any(old[field] is not None for field in CONFIRMATION_FIELDS):
            raise conflict("identity_sealed", "确认信息不能先于确认状态写入",
                           {"change_id": change.change_id})
    if change.status == "confirmed":
        _validate_confirmation(db, change)
    return append(db, EVENT_TYPE, change.change_id, revision, source_ref, body,
                  change.observed_at)


def confirmed_links(db: Connection, *, cut: int | None = None,
                    as_of: datetime | None = None, at: datetime | None = None,
                    scope: str | None = None) -> list[dict]:
    """返回在观测截点与业务时点上都已生效的已确认变更关系。"""
    instant = _business_time(at, as_of)
    result = []
    for row in _confirmed_records(db, cut=cut, as_of=as_of):
        value = row["payload"]
        if datetime.fromisoformat(value["effective_from"]) > instant:
            continue
        if scope is not None and scope not in value["scope"]:
            continue
        result.append({**value, "event_seq": row["seq"], "digest": row["digest"]})
    result.sort(key=lambda item: (item["effective_from"], item["event_seq"]))
    return result


def _outgoing(links: list[dict]) -> dict[str, dict]:
    value: dict[str, dict] = {}
    for link in links:
        value.setdefault(link["predecessor_id"], link)
    return value


def _head(provider_id: str, links: list[dict]) -> str:
    outgoing = _outgoing(links)
    seen = {provider_id}
    node = provider_id
    while node in outgoing:
        nxt = outgoing[node]["successor_id"]
        if nxt in seen:
            break
        seen.add(nxt)
        node = nxt
    return node


def _component(provider_id: str, links: list[dict]) -> list[str]:
    """返回与给定主体相连的整条经营身份链（可前后追溯）。"""
    adjacency: dict[str, set[str]] = {}
    for link in links:
        adjacency.setdefault(link["predecessor_id"], set()).add(link["successor_id"])
        adjacency.setdefault(link["successor_id"], set()).add(link["predecessor_id"])
    seen = {provider_id}
    queue = [provider_id]
    while queue:
        for neighbour in adjacency.get(queue.pop(0), ()):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return sorted(seen)


def _predecessors(provider_id: str, links: list[dict]) -> list[str]:
    incoming: dict[str, list[str]] = {}
    for link in links:
        incoming.setdefault(link["successor_id"], []).append(link["predecessor_id"])
    seen: set[str] = set()
    queue = [provider_id]
    while queue:
        for predecessor in incoming.get(queue.pop(0), ()):
            if predecessor not in seen:
                seen.add(predecessor)
                queue.append(predecessor)
    return sorted(seen - {provider_id})


def _chain(provider_id: str, links: list[dict]) -> list[dict]:
    members = set(_component(provider_id, links))
    return [link for link in links
            if link["successor_id"] in members and link["predecessor_id"] in members]


def resolve_from_links(provider_id: str, links: list[dict]) -> dict:
    """使用预先读取的已确认链路解析身份，避免逐个主体重复查询。"""
    supply_links = [link for link in links if "supply" in link["scope"]]
    case_links = [link for link in links if "cases" in link["scope"]]
    supply_head = _head(provider_id, supply_links)
    case_head = _head(provider_id, case_links)
    applied = {link["change_id"]: link for link in
               _chain(supply_head, supply_links) + _chain(case_head, case_links)}
    immediate = next((link for link in links
                      if link["predecessor_id"] == provider_id), None)
    is_head = supply_head == provider_id
    return {
        "requested_provider_id": provider_id,
        "current_provider_id": supply_head,
        "supply_current_provider_id": supply_head,
        "case_current_provider_id": case_head,
        "successor_provider_id": immediate["successor_id"] if immediate else None,
        "supply_superseded": not is_head,
        "cases_superseded": case_head != provider_id,
        "superseded": not is_head or case_head != provider_id,
        "supply_group_ids": _component(provider_id, supply_links),
        "previous_provider_ids": _predecessors(provider_id, supply_links),
        "case_previous_provider_ids": _predecessors(provider_id, case_links),
        "applied_change_ids": sorted(applied),
        "scope": sorted({scope for link in applied.values() for scope in link["scope"]}),
        "changes": [applied[key] for key in sorted(applied)],
    }


def resolve(db: Connection, provider_id: str, *, cut: int | None = None,
            as_of: datetime | None = None, at: datetime | None = None) -> dict:
    return resolve_from_links(provider_id,
                              confirmed_links(db, cut=cut, as_of=as_of, at=at))


def case_scope_ids(db: Connection, provider_id: str, *, cut: int | None = None,
                   as_of: datetime | None = None, at: datetime | None = None) -> set[str]:
    """查看某主体当前档案时应涵盖的历史主体编号（含自身）。"""
    links = [link for link in confirmed_links(db, cut=cut, as_of=as_of, at=at)
             if "cases" in link["scope"]]
    return set(_component(provider_id, links))


def linked_case_scope(db: Connection, case_provider_id: str, batch_provider_id: str, *,
                      cut: int | None = None, as_of: datetime | None = None,
                      at: datetime | None = None) -> bool:
    """线索主体与触达批次主体是否属于同一已确认的经营身份链。"""
    if case_provider_id == batch_provider_id:
        return True
    links = [link for link in confirmed_links(db, cut=cut, as_of=as_of, at=at)
             if "cases" in link["scope"]]
    return case_provider_id in _component(batch_provider_id, links)


def supply_superseded_ids(db: Connection, *, cut: int | None = None,
                          as_of: datetime | None = None,
                          at: datetime | None = None) -> set[str]:
    """仅当接续主体仍出现在当前供应商目录时，旧编号才退出当前供给计数。"""
    catalog_ids = {row["entity_id"] for row in current_entities(db, "provider",
                                                                cut=cut, as_of=as_of)}
    result = set()
    for link in confirmed_links(db, cut=cut, as_of=as_of, at=at, scope="supply"):
        if link["successor_id"] in catalog_ids:
            result.add(link["predecessor_id"])
    return result


def annotate_from_links(provider: dict, links: list[dict]) -> dict:
    resolution = resolve_from_links(provider["provider_id"], links)
    return {**provider,
            "current_provider_id": resolution["current_provider_id"],
            "previous_provider_ids": resolution["previous_provider_ids"],
            "identity": {"superseded": resolution["superseded"],
                         "supply_superseded": resolution["supply_superseded"],
                         "cases_superseded": resolution["cases_superseded"],
                         "successor_provider_id": resolution["successor_provider_id"],
                         "case_current_provider_id": resolution["case_current_provider_id"],
                         "applied_change_ids": resolution["applied_change_ids"]}}


def annotate(db: Connection, provider: dict, *, cut: int | None = None,
             as_of: datetime | None = None, at: datetime | None = None) -> dict:
    return annotate_from_links(
        provider, confirmed_links(db, cut=cut, as_of=as_of, at=at))


def changes(db: Connection, *, provider_id: str | None = None, cut: int | None = None,
            as_of: datetime | None = None, include_proposed: bool = True,
            at: datetime | None = None) -> list[dict]:
    instant = _business_time(at, as_of)
    result = []
    for row in current_entities(db, EVENT_TYPE, cut=cut, as_of=as_of):
        value = row["payload"]
        if not include_proposed and value["status"] != "confirmed":
            continue
        if provider_id and provider_id not in {value["predecessor_id"], value["successor_id"]}:
            continue
        effective = (value["status"] == "confirmed"
                     and datetime.fromisoformat(value["effective_from"]) <= instant)
        result.append({**value, "event_seq": row["seq"], "digest": row["digest"],
                       "effective": effective})
    result.sort(key=lambda item: (item["observed_at"], item["event_seq"]))
    return result


def provider_identity(db: Connection, provider_id: str, *, cut: int | None = None,
                      as_of: datetime | None = None, at: datetime | None = None) -> dict:
    if not any(row["entity_id"] == provider_id for row in current_entities(db, "provider")):
        raise missing("provider", provider_id)
    business_time = _business_time(at, as_of)
    return {"provider_id": provider_id,
            "as_of": business_time.isoformat(),
            "resolution": resolve(db, provider_id, cut=cut, as_of=as_of, at=business_time),
            "changes": changes(db, provider_id=provider_id, cut=cut, as_of=as_of,
                               at=business_time)}
