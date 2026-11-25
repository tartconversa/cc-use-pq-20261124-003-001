from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from silverwatch import (analysis, cases, identity, planning, queries,
                         registry, workqueue)
from silverwatch.api import create_app
from silverwatch.contracts import (AnalysisPolicy, Case, NeedBatch, Provider,
                                   ProviderIdentityChange, TouchBatch)
from silverwatch.errors import Problem
from tests.support import (DatabaseCase, case_data, instant, need_data, provider_data,
                          seed_case, touch_data)


def identity_data(**updates):
    value = {"change_id": "identity-one", "predecessor_id": "provider-one",
             "successor_id": "provider-two", "change_type": "rename",
             "effective_from": "2026-10-15T00:00:00Z", "scope": ["supply", "cases"],
             "status": "confirmed", "confirmed_by": "clerk-one",
             "confirmed_at": "2026-10-12T00:00:00Z", "evidence_ref": "registry://notice-one",
             "note": "Licence transferred after the operator renamed the service.",
             "source_ref": "registry-feed", "observed_at": "2026-10-13T00:00:00Z"}
    return value | updates


def successor_data(**updates):
    return provider_data(provider_id="provider-two", display_name="Community Meals (River)",
                         capacity_per_month=800, active_from="2026-10-15T00:00:00Z",
                         updated_at="2026-10-01T00:00:00Z") | updates


def policy(**updates):
    value = {"policy_id": "policy-one", "minimum_exposure": 100,
             "minimum_supply_coverage_bp": 0, "max_substantiated_rate_bp": 10_000,
             "severe_open_cases_block": False, "require_verified_remediation": False,
             "minimum_touch_coverage_bp": 0}
    return value | updates


class IdentityRegistrationTests(DatabaseCase, unittest.TestCase):
    def seed(self, db):
        registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
        return registry.register_provider(db, Provider.model_validate(successor_data()),
                                          "feed-b", 1)

    def test_proposed_change_is_recorded_but_never_merged(self):
        with self.transaction() as db:
            self.seed(db)
            body = ProviderIdentityChange.model_validate(identity_data(
                status="proposed", confirmed_by=None, confirmed_at=None, evidence_ref=None))
            registry.register_identity_change(db, body, "registry-feed", 1)
            self.assertEqual(identity.changes(db)[0]["status"], "proposed")
            catalog = {row["provider_id"]: row for row in registry.catalog(db)}
            self.assertFalse(catalog["provider-one"]["identity"]["superseded"])
            self.assertEqual({row["provider_id"] for row in registry.current_supply(db)},
                             {"provider-one", "provider-two"})
            self.assertEqual(identity.resolve(db, "provider-one")["current_provider_id"],
                             "provider-one")

    def test_confirmation_requires_evidence_and_a_preceding_time(self):
        with self.assertRaises(ValueError):
            ProviderIdentityChange.model_validate(identity_data(evidence_ref=None))
        with self.assertRaises(ValueError):
            ProviderIdentityChange.model_validate(identity_data(
                confirmed_at="2026-10-14T00:00:00Z", observed_at="2026-10-13T00:00:00Z"))
        with self.assertRaises(ValueError):
            ProviderIdentityChange.model_validate(identity_data(status="proposed",
                                                                confirmed_by="clerk-one"))

    def test_identity_change_requires_both_providers_to_be_registered(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            with self.assertRaises(Problem) as captured:
                registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                    identity_data()), "registry-feed", 1)
            self.assertEqual(captured.exception.code, "provider_missing")

    def test_confirmed_change_keeps_both_numbers_and_marks_the_old_one_superseded(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            catalog = {row["provider_id"]: row for row in registry.catalog(db)}
            self.assertEqual(set(catalog), {"provider-one", "provider-two"})
            self.assertTrue(catalog["provider-one"]["identity"]["superseded"])
            self.assertEqual(catalog["provider-one"]["current_provider_id"], "provider-two")
            self.assertFalse(catalog["provider-two"]["identity"]["superseded"])
            self.assertEqual([row["provider_id"] for row in registry.current_supply(db)],
                             ["provider-two"])

    def test_replaying_identical_change_is_idempotent_but_confirmed_changes_are_sealed(self):
        with self.transaction() as db:
            self.seed(db)
            body = ProviderIdentityChange.model_validate(identity_data())
            first = registry.register_identity_change(db, body, "registry-feed", 1)
            replay = registry.register_identity_change(db, body, "registry-feed", 1)
            self.assertEqual(first["seq"], replay["seq"])
            self.assertTrue(replay["replayed"])
            with self.assertRaises(Problem) as sealed:
                registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                    identity_data(note="A different account.")), "registry-feed", 1)
            self.assertEqual(sealed.exception.code, "identity_sealed")

    def test_change_scope_cannot_be_rewritten_after_registration(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data(status="proposed", confirmed_by=None, confirmed_at=None,
                              evidence_ref=None)), "registry-feed", 1)
            with self.assertRaises(Problem) as captured:
                registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                    identity_data(status="proposed", confirmed_by=None, confirmed_at=None,
                                  evidence_ref=None, scope=["supply"])), "registry-feed", 1)
            self.assertEqual(captured.exception.code, "identity_identity")

    def test_proposal_can_be_confirmed_without_creating_a_new_entity(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data(status="proposed", confirmed_by=None, confirmed_at=None,
                              evidence_ref=None)), "registry-feed", 1)
            confirmed = registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 2)
            self.assertFalse(confirmed["replayed"])
            self.assertEqual(identity.resolve(db, "provider-one")["current_provider_id"],
                             "provider-two")

    def test_confirmed_chain_cannot_branch_or_loop(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_provider(db, Provider.model_validate(
                provider_data(provider_id="provider-three")), "feed-c", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            with self.assertRaises(Problem) as branch:
                registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                    identity_data(change_id="identity-two", successor_id="provider-three")),
                    "registry-feed", 1)
            self.assertEqual(branch.exception.code, "identity_duplicate")
            with self.assertRaises(Problem) as loop:
                registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                    identity_data(change_id="identity-three", predecessor_id="provider-two",
                                  successor_id="provider-one")), "registry-feed", 1)
            self.assertEqual(loop.exception.code, "identity_cycle")

    def test_effective_time_controls_when_the_change_applies(self):
        with self.transaction() as db:
            self.seed(db)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            before = registry.current_supply(db, at=instant("2026-10-14T00:00:00Z"))
            self.assertEqual({row["provider_id"] for row in before},
                             {"provider-one", "provider-two"})
            after = registry.current_supply(db, at=instant("2026-10-16T00:00:00Z"))
            self.assertEqual([row["provider_id"] for row in after], ["provider-two"])

    def test_sequence_cut_reproduces_the_identity_before_confirmation(self):
        with self.transaction() as db:
            self.seed(db)
            cut = registry.source_cut(db)["cut"]
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            past = identity.resolve(db, "provider-one", cut=cut)
            now = identity.resolve(db, "provider-one")
            self.assertFalse(past["superseded"])
            self.assertEqual(past["current_provider_id"], "provider-one")
            self.assertTrue(now["superseded"])
            self.assertEqual(now["current_provider_id"], "provider-two")


class IdentitySupplyTests(DatabaseCase, unittest.TestCase):
    def seed_chain(self, db):
        registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=600)),
                                   "feed-a", 1)
        registry.register_provider(db, Provider.model_validate(successor_data(capacity_per_month=800)),
                                   "feed-b", 1)
        registry.register_need(db, NeedBatch.model_validate(need_data()))

    def confirm(self, db, **updates):
        registry.register_identity_change(db, ProviderIdentityChange.model_validate(
            identity_data(**updates)), "registry-feed", 1)

    def test_new_analysis_counts_current_supply_once_after_succession(self):
        with self.transaction() as db:
            self.seed_chain(db)
            self.confirm(db)
            document = analysis.run(db, cut=registry.source_cut(db)["cut"],
                                    as_of=instant("2026-11-02T00:00:00Z"), region="suburban",
                                    service_kind="meal", cohort=None,
                                    policy=AnalysisPolicy.model_validate(policy()))
            self.assertEqual(document["supply"]["meal"]["monthly_capacity"], 800)
            self.assertEqual(document["supply"]["meal"]["capacity_providers"], ["provider-two"])

    def test_analysis_before_effective_date_still_counts_the_old_provider(self):
        with self.transaction() as db:
            self.seed_chain(db)
            self.confirm(db)
            document = analysis.run(db, cut=registry.source_cut(db)["cut"],
                                    as_of=instant("2026-10-16T00:00:00Z"), region="suburban",
                                    service_kind="meal", cohort=None,
                                    policy=AnalysisPolicy.model_validate(policy()))
            self.assertEqual(document["supply"]["meal"]["monthly_capacity"], 800)
            self.assertEqual(document["supply"]["meal"]["capacity_providers"], ["provider-two"])
            earlier = analysis.run(db, cut=registry.source_cut(db)["cut"],
                                   as_of=instant("2026-10-10T00:00:00Z"), region="suburban",
                                   service_kind="meal", cohort=None,
                                   policy=AnalysisPolicy.model_validate(policy()))
            self.assertEqual(earlier["supply"]["meal"]["monthly_capacity"], 600)
            self.assertEqual(earlier["supply"]["meal"]["capacity_providers"], ["provider-one"])

    def test_planning_counts_each_capacity_once(self):
        with self.transaction() as db:
            self.seed_chain(db)
            self.confirm(db)
            plan = planning.plan_region(db, region="suburban",
                                        period_end=instant("2026-11-01T00:00:00Z"))
            self.assertEqual(plan["totals"]["monthly_capacity"], 800)
            self.assertEqual(plan["areas"][0]["provider_ids"], ["provider-two"])
            earlier = planning.plan_region(db, region="suburban",
                                           period_end=instant("2026-10-10T00:00:00Z"))
            self.assertEqual(earlier["totals"]["monthly_capacity"], 600)

    def test_proposed_succession_never_reduces_current_capacity(self):
        with self.transaction() as db:
            self.seed_chain(db)
            self.confirm(db, status="proposed", confirmed_by=None, confirmed_at=None,
                         evidence_ref=None)
            plan = planning.plan_region(db, region="suburban",
                                        period_end=instant("2026-11-01T00:00:00Z"))
            self.assertEqual(plan["totals"]["monthly_capacity"], 1400)


class IdentityHistoryAndCaseTests(DatabaseCase, unittest.TestCase):
    def seed_chain(self, db):
        seed_case(db)
        registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
        registry.register_identity_change(db, ProviderIdentityChange.model_validate(
            identity_data()), "registry-feed", 1)

    def test_analysis_records_which_identity_changes_it_applied(self):
        with self.transaction() as db:
            seed_case(db)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            document = analysis.run(db, cut=registry.source_cut(db)["cut"],
                                    as_of=instant("2026-11-02T00:00:00Z"), region="suburban",
                                    service_kind="meal", cohort=None,
                                    policy=AnalysisPolicy.model_validate(policy()))
            self.assertEqual(document["identity"]["effective_change_ids"], ["identity-one"])
            self.assertEqual(document["identity"]["superseded_provider_ids"], ["provider-one"])

    def test_public_projection_hides_provider_ids_from_the_identity_summary(self):
        from silverwatch.exports import public_view
        bulletin = {"edition": 1, "document": {
            "policy": {"minimum_exposure": 20},
            "areas": [{"district": "district-a", "service_kind": "meal", "cohort": "70_79",
                       "exposed": 200, "reports": 1, "gaps": []}],
            "risk_flags": [],
            "supply": {"meal": {"estimated_people": 100, "monthly_capacity": 80,
                                "capacity_ratio": "8000bp"}},
            "case_counts": {"substantiated": 1, "pending": 0, "inconclusive": 0},
            "identity": {"effective_change_ids": ["identity-one"],
                         "superseded_provider_ids": ["provider-one"],
                         "note": "internal"}}}
        view = public_view(bulletin)["document"]
        self.assertNotIn("provider-one", str(view))
        self.assertEqual(view["identity_scope"], {"effective_change_count": 1,
                                                  "superseded_provider_count": 1})

    def test_old_bulletin_keeps_the_pre_change_interpretation(self):
        with self.transaction() as db:
            seed_case(db)
            policy_model = AnalysisPolicy.model_validate(policy())
            cut = registry.source_cut(db)["cut"]
            before = analysis.run(db, cut=cut, as_of=instant("2026-11-02T00:00:00Z"),
                                  region="suburban", service_kind="meal", cohort=None,
                                  policy=policy_model)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            after = analysis.run(db, cut=registry.source_cut(db)["cut"],
                                 as_of=instant("2026-11-02T00:00:00Z"), region="suburban",
                                 service_kind="meal", cohort=None, policy=policy_model)
            self.assertEqual(before["supply"]["meal"]["capacity_providers"], ["provider-one"])
            self.assertEqual(after["supply"]["meal"]["capacity_providers"], ["provider-two"])
            replay = analysis.run(db, cut=cut, as_of=instant("2026-11-02T00:00:00Z"),
                                  region="suburban", service_kind="meal", cohort=None,
                                  policy=policy_model)
            self.assertEqual(replay["supply"], before["supply"])

    def test_case_tracked_under_old_provider_appears_in_current_profile(self):
        with self.transaction() as db:
            self.seed_chain(db)
            profile = queries.provider_profile(db, "provider-two")
            self.assertEqual(profile["related_provider_ids"], ["provider-one", "provider-two"])
            self.assertEqual([row["case"]["case_id"] for row in profile["cases"]], ["case-one"])
            self.assertEqual(profile["identity"]["resolution"]["current_provider_id"],
                             "provider-two")

    def test_historical_profile_scope_reproduces_the_old_number_only(self):
        with self.transaction() as db:
            self.seed_chain(db)
            profile = queries.provider_profile(db, "provider-one", scope="historical")
            self.assertEqual(profile["related_provider_ids"], ["provider-one"])
            self.assertEqual(profile["activity"]["case_count"], 1)
            self.assertEqual(profile["activity"]["open_case_count"], 1)

    def test_pending_work_links_the_original_number_to_the_current_one(self):
        with self.transaction() as db:
            self.seed_chain(db)
            queue = workqueue.investigation_queue(db, as_of=instant("2026-11-02T00:00:00Z"))
            row = queue["items"][0]
            self.assertEqual(row["provider_id"], "provider-one")
            self.assertEqual(row["current_provider_id"], "provider-two")
            self.assertEqual([item["status"] for item in row["identity_changes"]],
                             ["confirmed"])

    def test_case_number_alone_does_not_change_when_a_change_is_unconfirmed(self):
        with self.transaction() as db:
            seed_case(db)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data(status="proposed", confirmed_by=None, confirmed_at=None,
                              evidence_ref=None)), "registry-feed", 1)
            profile = queries.provider_profile(db, "provider-two")
            self.assertEqual(profile["related_provider_ids"], ["provider-two"])
            self.assertEqual(profile["cases"], [])

    def test_case_can_be_raised_against_either_number_in_a_confirmed_chain(self):
        with self.transaction() as db:
            seed_case(db)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            later = case_data(case_id="case-two", provider_id="provider-two",
                              reported_at="2026-10-20T09:00:00Z")
            cases.create(db, Case.model_validate(later), "case-feed", 1)
            self.assertEqual(
                {row["case"]["case_id"] for row in queries.provider_profile(db, "provider-one")["cases"]},
                {"case-one", "case-two"})

    def test_unrelated_provider_cannot_claim_a_case_in_the_chain(self):
        with self.transaction() as db:
            seed_case(db)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_provider(db, Provider.model_validate(
                provider_data(provider_id="provider-three")), "feed-c", 1)
            with self.assertRaises(Problem) as captured:
                cases.create(db, Case.model_validate(case_data(case_id="case-x",
                             provider_id="provider-three")), "case-feed", 1)
            self.assertEqual(captured.exception.code, "case_scope")
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            with self.assertRaises(Problem) as still_wrong:
                cases.create(db, Case.model_validate(case_data(case_id="case-y",
                             provider_id="provider-three")), "case-feed", 1)
            self.assertEqual(still_wrong.exception.code, "case_scope")

    def test_scoped_change_applies_only_to_the_declared_use(self):
        with self.transaction() as db:
            seed_case(db)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data(scope=["supply"])), "registry-feed", 1)
            resolution = identity.resolve(db, "provider-one")
            self.assertTrue(resolution["supply_superseded"])
            self.assertFalse(resolution["cases_superseded"])
            self.assertEqual(resolution["case_current_provider_id"], "provider-one")
            profile = queries.provider_profile(db, "provider-two")
            self.assertEqual(profile["related_provider_ids"], ["provider-two"])

    def test_case_scope_follows_the_reported_date_not_todays_identity(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            registry.register_provider(db, Provider.model_validate(successor_data()), "feed-b", 1)
            registry.register_touch(db, TouchBatch.model_validate(touch_data()))
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "registry-feed", 1)
            early = case_data(case_id="case-early", provider_id="provider-two",
                              reported_at="2026-10-10T09:00:00Z")
            with self.assertRaises(Problem) as captured:
                cases.create(db, Case.model_validate(early), "case-feed", 1)
            self.assertEqual(captured.exception.code, "case_scope")
            late = case_data(case_id="case-late", provider_id="provider-two",
                             reported_at="2026-10-20T09:00:00Z")
            cases.create(db, Case.model_validate(late), "case-feed", 1)
            self.assertEqual(
                {row["case"]["case_id"] for row in queries.provider_profile(db, "provider-one")["cases"]},
                {"case-late"})

    def test_intake_bundle_accepts_identity_changes_in_one_transaction(self):
        from silverwatch import intake
        with self.transaction() as db:
            bundle = {"batch_id": "identity-bundle", "source_ref": "registry", "revision": 1,
                      "records": [{"kind": "providers", "data": provider_data()},
                                  {"kind": "providers", "data": successor_data()},
                                  {"kind": "identity_changes", "data": identity_data()}]}
            result = intake.import_bundle(db, bundle)
            self.assertEqual(result["accepted_count"], 3)
            self.assertEqual(identity.resolve(db, "provider-one")["current_provider_id"],
                             "provider-two")

    def test_intake_bundle_rolls_back_an_unconfirmed_duplicate_succession(self):
        from silverwatch import intake
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "a", 1)
            registry.register_provider(db, Provider.model_validate(successor_data()), "b", 1)
            registry.register_provider(db, Provider.model_validate(
                provider_data(provider_id="provider-three")), "c", 1)
            registry.register_identity_change(db, ProviderIdentityChange.model_validate(
                identity_data()), "r", 1)
            bundle = {"batch_id": "identity-bundle", "source_ref": "registry", "revision": 1,
                      "records": [{"kind": "identity_changes", "data": identity_data(
                          change_id="identity-two", successor_id="provider-three")}]}
            with self.assertRaises(Problem):
                intake.import_bundle(db, bundle)
            self.assertEqual(len(identity.changes(db)), 1)


class IdentityHttpTests(DatabaseCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.store.location))

    def test_identity_routes_register_list_and_explain(self):
        for body, source in ((provider_data(), "feed-a"), (successor_data(), "feed-b")):
            response = self.client.post(f"/providers?source_ref={source}&revision=1", json=body)
            self.assertEqual(response.status_code, 200)
        created = self.client.post("/provider-identities?source_ref=registry&revision=1",
                                   json=identity_data())
        self.assertEqual(created.status_code, 200)
        listing = self.client.get("/provider-identities?provider_id=provider-one")
        self.assertEqual(listing.json()["count"], 1)
        identity_view = self.client.get("/providers/provider-one/identity")
        self.assertEqual(identity_view.json()["resolution"]["current_provider_id"],
                         "provider-two")

    def test_identity_import_kind_is_supported_by_bundle_validation(self):
        bundle = {"batch_id": "identity-bundle", "source_ref": "registry", "revision": 1,
                  "records": [{"kind": "providers", "data": provider_data()},
                              {"kind": "providers", "data": successor_data()},
                              {"kind": "identity_changes", "data": identity_data()}]}
        response = self.client.post("/imports/validate", json=bundle)
        self.assertTrue(response.json()["valid"])
        imported = self.client.post("/imports", json=bundle)
        self.assertEqual(imported.json()["accepted_count"], 3)

    def test_unknown_profile_scope_is_rejected(self):
        response = self.client.post("/providers?source_ref=feed&revision=1", json=provider_data())
        self.assertEqual(response.status_code, 200)
        invalid_scope = self.client.get("/providers/provider-one?scope=everything")
        self.assertEqual(invalid_scope.status_code, 422)
        self.assertEqual(invalid_scope.json()["code"], "provider_scope")


if __name__ == "__main__":
    unittest.main()
