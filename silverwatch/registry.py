from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from pydantic import ValidationError

from silverwatch import identity
from silverwatch.contracts import NeedBatch, Provider, TouchBatch
from silverwatch.errors import invalid
from silverwatch.ledger import append, current_entities, latest


def register_provider(db: Connection, provider: Provider, source_ref: str, revision: int) -> dict:
    return append(db, "provider", provider.provider_id, revision, source_ref,
                  provider.model_dump(mode="json"), provider.updated_at)


def register_identity_change(db: Connection, change, source_ref: str, revision: int) -> dict:
    """登记经营身份变化；前后主体都必须已登记，未确认记录只登记不合并。"""
    return identity.register(db, change, source_ref, revision)


def register_touch(db: Connection, batch: TouchBatch) -> dict:
    providers = latest(db, "provider")
    if not any(row["entity_id"] == batch.provider_id for row in providers):
        raise invalid("provider_missing", "触达批次的服务提供方尚未登记")
    return append(db, "touch", batch.batch_id, batch.source_revision,
                  batch.source_ref, batch.model_dump(mode="json"), batch.observed_at)


def register_need(db: Connection, batch: NeedBatch) -> dict:
    return append(db, "need", batch.estimate_id, batch.source_revision,
                  batch.source_ref, batch.model_dump(mode="json"), batch.period_end)


def register_case(db: Connection, body: dict, source_ref: str, revision: int) -> dict:
    required = {"case_id", "provider_id", "batch_id", "kind", "severity", "reported_at",
                "allegation", "source_ref", "affected_people", "status"}
    if set(body) != required:
        raise invalid("case_shape", "线索记录字段不完整或包含未支持字段")
    try:
        from silverwatch.contracts import Case

        case = Case.model_validate(body)
    except ValidationError as exc:
        raise invalid("case_invalid", "线索记录不符合业务字段", exc.errors(include_url=False)) from exc
    if not any(row["entity_id"] == case.provider_id for row in current_entities(db, "provider")):
        raise invalid("provider_missing", "线索指向的服务提供方尚未登记")
    if not any(row["entity_id"] == case.batch_id for row in current_entities(db, "touch")):
        raise invalid("touch_missing", "线索分母批次尚未登记")
    batch = next(row["payload"] for row in current_entities(db, "touch") if row["entity_id"] == case.batch_id)
    if case.affected_people > batch["exposed_people"] + batch["nonresponse_people"]:
        raise invalid("case_population", "影响人数不能超过触达及未回应总人数")
    if not identity.linked_case_scope(db, case.provider_id, batch["provider_id"],
                                      at=case.reported_at):
        raise invalid("case_scope", "线索归属与触达批次不一致")
    return append(db, "case", case.case_id, revision, source_ref,
                  case.model_dump(mode="json"), case.reported_at)


def catalog(db: Connection, *, region: str | None = None, cut: int | None = None,
            as_of: datetime | None = None, at: datetime | None = None) -> list[dict]:
    """按截点读取服务方档案，并标明其当前接续主体（不替换原编号）。"""
    providers = current_entities(db, "provider", cut=cut, as_of=as_of)
    links = identity.confirmed_links(db, cut=cut, as_of=as_of, at=at)
    result = []
    for row in providers:
        value = row["payload"]
        if region and value["region"] != region:
            continue
        result.append(identity.annotate_from_links({
            **value, "source_revision": row["revision"], "source_ref": row["source_ref"],
            "event_seq": row["seq"], "digest": row["digest"]}, links))
    return result


def current_supply(db: Connection, *, region: str | None = None, cut: int | None = None,
                   as_of: datetime | None = None, at: datetime | None = None) -> list[dict]:
    """当前有效供给：已确认的接续关系只保留接续链末端，旧编号不再重复计数。"""
    rows = catalog(db, region=region, cut=cut, as_of=as_of, at=at)
    superseded = identity.supply_superseded_ids(db, cut=cut, as_of=as_of, at=at)
    return [row for row in rows if row["provider_id"] not in superseded]


def touch_facts(db: Connection, *, region: str | None = None, cut: int | None = None,
                as_of: datetime | None = None) -> list[dict]:
    result = []
    for row in current_entities(db, "touch", cut=cut, as_of=as_of):
        value = row["payload"]
        if region and value["region"] != region:
            continue
        result.append({**value, "event_seq": row["seq"], "digest": row["digest"]})
    return result


def needs(db: Connection, *, region: str | None = None, cut: int | None = None,
          as_of: datetime | None = None) -> list[dict]:
    result = []
    for row in current_entities(db, "need", cut=cut, as_of=as_of):
        value = row["payload"]
        if as_of is not None and datetime.fromisoformat(value["period_end"]) > as_of:
            continue
        if region is None or value["region"] == region:
            result.append({**value, "event_seq": row["seq"], "digest": row["digest"]})
    return result


def provider_capacity(db: Connection, *, region: str, service_kind: str,
                      at: datetime, cut: int | None = None) -> list[dict]:
    """返回所选时点仍在服务有效期内的机构，并按身份接续去重。"""
    candidates = current_supply(db, region=region, cut=cut, at=at)
    result = []
    instant = at.astimezone(timezone.utc)
    for provider in candidates:
        if provider["service_kind"] != service_kind or provider["status"] != "active":
            continue
        start = datetime.fromisoformat(provider["active_from"])
        end = datetime.fromisoformat(provider["active_until"]) if provider["active_until"] else None
        if start <= instant and (end is None or instant < end):
            result.append(provider)
    return result


def registry_summary(db: Connection, *, region: str, cut: int | None = None) -> dict:
    providers = catalog(db, region=region, cut=cut)
    current_ids = {row["provider_id"] for row in current_supply(db, region=region, cut=cut)}
    by_kind: dict[str, dict] = {}
    for row in providers:
        group = by_kind.setdefault(row["service_kind"], {"providers": 0, "capacity": 0,
                                                          "active": 0, "suspended": 0,
                                                          "closed": 0, "superseded": 0,
                                                          "current": 0})
        group["providers"] += 1
        group[row["status"]] += 1
        group["superseded"] += int(row["identity"]["supply_superseded"])
        if row["provider_id"] in current_ids:
            group["current"] += 1
            group["capacity"] += row["capacity_per_month"]
    return {"region": region, "provider_count": len(providers),
            "current_provider_count": len(current_ids),
            "service_kinds": dict(sorted(by_kind.items())),
            "cut": cut if cut is not None else source_cut(db)["cut"]}


def identity_changes_for_case(db: Connection, case: dict, *, cut: int | None = None,
                              at: datetime | None = None) -> list[dict]:
    """列出影响该线索解释的经营身份变化，区分已确认与待确认。"""
    result = []
    for link in identity.changes(db, cut=cut, at=at):
        if case["provider_id"] not in {link["predecessor_id"], link["successor_id"]}:
            continue
        result.append({"change_id": link["change_id"], "status": link["status"],
                       "change_type": link["change_type"],
                       "predecessor_id": link["predecessor_id"],
                       "successor_id": link["successor_id"],
                       "scope": link["scope"], "effective": link["effective"]})
    return result


def source_cut(db: Connection) -> dict:
    from silverwatch.ledger import current_cut

    return {"cut": current_cut(db), "captured_at": datetime.now(timezone.utc).isoformat()}
