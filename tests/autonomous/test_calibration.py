import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from decimal import Decimal

from bot.autonomous.calibration import (attest_calibration_artifact, build_calibration_artifact, binary_brier,
    OutcomeEvidenceVerifier, VerifiedOutcomeEvidence,
    classify_terminal_direction, direction_brier, estimate_edge, fixed_horizon_maturity,
    fourth_completed_hour_close_after, label_mature,
    resolved_brier, wilson_lower, HOUR_US)
from bot.autonomous.contracts import (Regime, UnavailableEdge, PersistenceUnavailable, ContractViolation, CandidateDecision,
    DecisionBatch, Direction, DirectionDistribution, AdvisoryRisk, content_hash,
    AccountRevision, CausalCutoffs, EdgeEstimate, Feature, NumericSnapshot, canonical_bytes, strict_json)
from bot.autonomous.ledger import Ledger


class CalibrationTests(unittest.TestCase):
    def test_verified_execution_math_requires_bound_issuer_and_complete_horizon(self):
        from bot.autonomous.calibration import (OutcomeEvidenceVerifier, OutcomeVerificationRequest,
            VerifiedOutcomeEvidence, verified_outcome_payload)

        class SyntheticVerifier(OutcomeEvidenceVerifier):
            def verify(self, request):
                return None

        verifier=SyntheticVerifier()
        request=OutcomeVerificationRequest("label","forecast","decision",1,"buy","model-v1",
            "a"*64,"b"*64,"c"*64,10*HOUR_US,10*HOUR_US+1,15*HOUR_US,"previous","{}")
        evidence=VerifiedOutcomeEvidence(verifier,request.request_hash,
            10*HOUR_US+100,10*HOUR_US+101,10*HOUR_US+102,
            tuple(i*HOUR_US for i in (11,12,13,14)),14*HOUR_US,
            14*HOUR_US+1,14*HOUR_US+2,14*HOUR_US+3,
            Decimal("2"),Decimal("100"),Decimal("101"),
            Decimal(".5"),Decimal(".5"),Decimal(".25"),"d"*64,
            ("e"*64,),None)
        payload=verified_outcome_payload(request,evidence,verifier)
        self.assertEqual("0.00375",payload["net_return"])
        self.assertEqual(1,payload["net_positive_outcome"])
        self.assertEqual({"base_quantity":"2","entry_price_usd":"100","exit_price_usd":"101",
            "entry_fee_usd":"0.5","exit_fee_usd":"0.5","slippage_cost_usd":"0.25"},
            payload["verified_execution"])
        self.assertEqual("quantity_times_price_change_minus_explicit_costs_v1",payload["pnl_method"])
        losing=verified_outcome_payload(request,replace(evidence,slippage_cost_usd=Decimal("2")),verifier)
        self.assertEqual("-0.005",losing["net_return"])
        self.assertEqual(0,losing["net_positive_outcome"])
        for invalid in (replace(evidence,issuer=SyntheticVerifier()),
                replace(evidence,request_hash="f"*64),
                replace(evidence,hourly_closes_us=evidence.hourly_closes_us[:-1]),
                replace(evidence,hourly_closes_us=(11*HOUR_US,12*HOUR_US,14*HOUR_US,15*HOUR_US)),
                replace(evidence,entry_event_us=request.forecast_at_us),
                replace(evidence,verified_at_us=request.as_of_us+1),
                replace(evidence,execution_manifest_hash="not-a-hash")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                verified_outcome_payload(request,invalid,verifier)

    def test_verified_return_projection_ignores_callers_decimal_traps(self):
        from decimal import localcontext, Inexact, Rounded, ROUND_UP
        from bot.autonomous.calibration import (OutcomeEvidenceVerifier, OutcomeVerificationRequest,
            VerifiedOutcomeEvidence, verified_outcome_payload)

        class SyntheticVerifier(OutcomeEvidenceVerifier):
            def verify(self, request):
                return None

        verifier=SyntheticVerifier()
        request=OutcomeVerificationRequest("label","forecast","decision",1,"buy","model-v1",
            "a"*64,"b"*64,"c"*64,10*HOUR_US,10*HOUR_US+1,15*HOUR_US,"previous","{}")
        evidence=VerifiedOutcomeEvidence(verifier,request.request_hash,
            10*HOUR_US+100,10*HOUR_US+101,10*HOUR_US+102,
            tuple(i*HOUR_US for i in (11,12,13,14)),14*HOUR_US,
            14*HOUR_US+1,14*HOUR_US+2,14*HOUR_US+3,
            Decimal("1"),Decimal("3"),Decimal("4"),
            Decimal("0"),Decimal("0"),Decimal("0"),"d"*64,("e"*64,),None)
        baseline=verified_outcome_payload(request,evidence,verifier)
        with localcontext() as caller:
            caller.prec=2
            caller.rounding=ROUND_UP
            caller.traps[Inexact]=True
            caller.traps[Rounded]=True
            under_traps=verified_outcome_payload(request,evidence,verifier)
        self.assertEqual(baseline,under_traps)
        self.assertEqual("0."+"3"*38,under_traps["net_return"])
        self.assertEqual(("1","3"),under_traps["net_return_exact"])

    def _observations(self, prefix, n, *, start_us, probabilities=(.1, .3, .6, .9)):
        rows=[]
        for i in range(n):
            decision=start_us+i*5*HOUR_US
            quote=decision+100
            horizon=(quote//HOUR_US+4)*HOUR_US
            outcome=int(i%10<7)
            rows.append({"independent_id":f"{prefix}-{i}", "decision_at_us":decision,
                "forecast_id":f"forecast-{prefix}-{i}",
                "label_event_id":"label-event-"+content_hash((prefix,i)),
                "forecast_at_us":decision+1,"first_executable_quote_us":quote,
                "horizon_close_us":horizon,"resolved_at_us":horizon+1,
                "direction_probability":probabilities[i%len(probabilities)],
                "net_positive_outcome":outcome,"terminal_direction_outcome":("long" if i%2 else "neutral"),
                "net_return":Decimal("0.01") if outcome else Decimal("-0.02"),
                "costs_included":True,"execution_manifest_hash":"f"*64,
                "observations":{"quote":True,"depth":True,"fees":True,
                    "slippage":True,"matured_data":True},"side":"buy",
                "instrument":"BTC-USD","regime":Regime.TRENDING.value,
                "model_id":"model-v1","schema_hash":"a"*64,
                "dictionary_hash":"b"*64,"label_hash":"c"*64,"cost_hash":"d"*64})
        return rows

    def _artifact(self, *, calibration=None, training=None):
        training=training or self._observations("train",400,start_us=1_000_000_000_000)
        train_cutoff=max(r["resolved_at_us"] for r in training)+1
        calibration=calibration or self._observations("cal",400,start_us=train_cutoff+HOUR_US)
        calibration_cutoff=max(r["resolved_at_us"] for r in calibration)+1
        return build_calibration_artifact(training=training,calibration=calibration,
            model_id="model-v1",schema_hash="a"*64,dictionary_hash="b"*64,
            label_hash="c"*64,cost_hash="d"*64,instrument="BTC-USD",
            regime=Regime.TRENDING,training_cutoff_us=train_cutoff,
            calibration_cutoff_us=calibration_cutoff,valid_from_us=calibration_cutoff+1,
            expires_at_us=calibration_cutoff+HOUR_US)

    def test_brier_and_invalid_or_pending_labels(self):
        self.assertAlmostEqual(binary_brier((.8,.3),(1,0)),.065)
        with self.assertRaises(ValueError): binary_brier((.8,),())
        with self.assertRaises(ValueError): binary_brier((.8,),(2,))
        for value in (math.nan,math.inf,True,-.1,1.1):
            with self.assertRaises(ValueError): binary_brier((value,),(1,))
        score,count,missing=direction_brier(((.8,.1,.1),(.2,.7,.1),(.1,.1,.8),(.4,.4,.2)),("long","short","neutral",None))
        self.assertAlmostEqual((.06+.14+.06)/3,score)
        self.assertEqual((3,1),(count,missing))
        with self.assertRaises(ValueError): direction_brier(((.5,.5,.1),),("neutral",))
        self.assertEqual("long",classify_terminal_direction(Decimal(".01"),Decimal("-.02")))
        self.assertEqual("neutral",classify_terminal_direction(Decimal("-.01"),Decimal("0")))
        with self.assertRaises(ValueError): classify_terminal_direction(Decimal(".01"),Decimal(".02"))

    def test_maturity_wilson_and_missingness(self):
        self.assertFalse(label_mature(101,100)); self.assertTrue(label_mature(100,100))
        self.assertEqual(14_400_000_000,fixed_horizon_maturity(100,4))
        self.assertEqual(14*HOUR_US,fourth_completed_hour_close_after(10*HOUR_US+HOUR_US//2))
        self.assertEqual(14*HOUR_US,fourth_completed_hour_close_after(10*HOUR_US))
        with self.assertRaises(ValueError): fixed_horizon_maturity(100,3)
        self.assertAlmostEqual(.2,resolved_brier((.8,.4,.6),(1,None,0))[0])
        self.assertEqual((2,1),resolved_brier((.8,.4,.6),(1,None,0))[1:])
        self.assertEqual((None,0,1),resolved_brier((None,),(1,)))
        self.assertGreater(wilson_lower(70,100),.6)

    def test_training_only_bins_and_artifact_stays_disabled_without_review(self):
        artifact=self._artifact()
        shifted=self._observations("shift",400,start_us=max(r["resolved_at_us"] for r in self._observations("train",400,start_us=1_000_000_000_000))+1+HOUR_US,
            probabilities=(.05,.25,.65,.95))
        other=self._artifact(calibration=shifted)
        self.assertEqual([(b.lower,b.upper) for b in artifact.mapping],[(b.lower,b.upper) for b in other.mapping])
        self.assertFalse(artifact.uncertainty_passed or artifact.tail_checks_passed)

    def test_artifact_identity_binds_exact_training_and_calibration_rows(self):
        training=self._observations("train",400,start_us=1_000_000_000_000)
        cutoff=max(r["resolved_at_us"] for r in training)+1
        calibration=self._observations("cal",400,start_us=cutoff+HOUR_US)
        original=self._artifact(training=training,calibration=calibration)
        training[0]["execution_manifest_hash"]="e"*64
        revised_training=self._artifact(training=training,calibration=calibration)
        self.assertNotEqual(original.artifact_hash,revised_training.artifact_hash)
        calibration[0]["label_event_id"]="label-event-"+"d"*64
        revised_labels=self._artifact(training=training,calibration=calibration)
        self.assertNotEqual(revised_training.artifact_hash,revised_labels.artifact_hash)

    def test_reused_forecast_or_label_identity_cannot_count_as_independent(self):
        for identity in ("forecast_id","label_event_id"):
            with self.subTest(identity=identity):
                training=self._observations("train",400,start_us=1_000_000_000_000)
                cutoff=max(r["resolved_at_us"] for r in training)+1
                calibration=self._observations("cal",400,start_us=cutoff+HOUR_US)
                calibration[0][identity]=training[0][identity]
                with self.assertRaises(ValueError):
                    self._artifact(training=training,calibration=calibration)

    def test_negative_causal_timestamps_are_rejected(self):
        training=self._observations("train",400,start_us=1_000_000_000_000)
        training[0].update(decision_at_us=-5*HOUR_US,forecast_at_us=-5*HOUR_US+1,
            first_executable_quote_us=-5*HOUR_US+100,horizon_close_us=-HOUR_US,
            resolved_at_us=-HOUR_US+1)
        with self.assertRaises(ValueError):
            self._artifact(training=training)

    def test_insufficient_bin_support_mixed_versions_and_missing_costs_reject(self):
        training=self._observations("train",400,start_us=1_000_000_000_000)
        cutoff=max(r["resolved_at_us"] for r in training)+1
        with self.assertRaisesRegex(ValueError,"100 independent"):
            self._artifact(training=training,calibration=self._observations("too-small",396,start_us=cutoff+HOUR_US))
        for field,value in (("model_id","another-model"),("schema_hash","e"*64),
                ("dictionary_hash","e"*64),("label_hash","e"*64),("cost_hash","e"*64),
                ("instrument","ETH-USD"),("regime","crisis"),("costs_included",False)):
            with self.subTest(field=field):
                calibration=self._observations("cal",400,start_us=cutoff+HOUR_US)
                calibration[0][field]=value
                with self.assertRaises(ValueError):
                    self._artifact(training=training,calibration=calibration)

    def test_fourth_hour_horizon_rejects_three_candles_and_late_horizons(self):
        training=self._observations("train",400,start_us=1_000_000_000_000)
        cutoff=max(r["resolved_at_us"] for r in training)+1
        for offset in (-HOUR_US,1,HOUR_US):
            with self.subTest(offset=offset):
                calibration=self._observations("cal",400,start_us=cutoff+HOUR_US)
                calibration[0]["horizon_close_us"]+=offset
                calibration[0]["resolved_at_us"]=calibration[0]["horizon_close_us"]+1
                with self.assertRaises(ValueError):
                    self._artifact(training=training,calibration=calibration)

    def test_missing_execution_observations_and_confidence_substitution_reject(self):
        training=self._observations("train",400,start_us=1_000_000_000_000)
        cutoff=max(r["resolved_at_us"] for r in training)+1
        for field in ("quote","depth","fees","slippage","matured_data"):
            with self.subTest(field=field):
                calibration=self._observations("cal",400,start_us=cutoff+HOUR_US)
                calibration[0]["observations"].pop(field)
                with self.assertRaises(ValueError):
                    self._artifact(training=training,calibration=calibration)
        calibration=self._observations("cal",400,start_us=cutoff+HOUR_US)
        calibration[0].pop("direction_probability")
        calibration[0]["confidence"]=.99
        with self.assertRaises(ValueError):
            self._artifact(training=training,calibration=calibration)

    def test_overlapping_windows_future_decisions_and_nonmature_labels_rejected(self):
        training=self._observations("train",120,start_us=1_000_000_000_000)
        train_cutoff=max(r["resolved_at_us"] for r in training)+1
        calibration=self._observations("cal",120,start_us=train_cutoff+HOUR_US)
        calibration[1]["first_executable_quote_us"]=calibration[0]["first_executable_quote_us"]+1
        quote=calibration[1]["first_executable_quote_us"]
        calibration[1]["horizon_close_us"]=(quote//HOUR_US+4)*HOUR_US
        with self.assertRaises(ValueError): self._artifact(training=training,calibration=calibration)
        calibration=self._observations("cal2",120,start_us=train_cutoff+HOUR_US)
        calibration[0]["decision_at_us"]=train_cutoff
        with self.assertRaises(ValueError): self._artifact(training=training,calibration=calibration)
        calibration=self._observations("cal3",120,start_us=train_cutoff+HOUR_US)
        calibration[0]["resolved_at_us"]=calibration[0]["horizon_close_us"]-1
        with self.assertRaises(ValueError): self._artifact(training=training,calibration=calibration)

    def test_edge_estimation_rejects_mappings_and_missing_review_or_persistence(self):
        artifact=self._artifact()
        self.assertIsInstance(estimate_edge({}, {}, None, artifact),UnavailableEdge)
        self.assertIsInstance(estimate_edge(None,None,None,None),UnavailableEdge)
        self.assertFalse(artifact.uncertainty_passed)

    def test_forecast_requires_saved_typed_judgment_and_labels_stay_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=Ledger.open(Path(directory)/"ledger.sqlite3")
            try:
                ledger.create_paper_account("paper-calibration")
                now=20_000_000_000; ledger._clock_us=lambda:now
                release=content_hash({})
                ledger.connection.execute("INSERT INTO release_bundles VALUES (?,?,NULL,?,?)",(release,"{}",now+100_000_000,now))
                close=now-1000
                ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("snapshot-cal","paper-calibration",release,close,close,"b"*64,"{}","d"*64,0,"c"*64,"{}",1,None,now))
                judgment=CandidateDecision("aave",Regime.TRENDING,Direction.LONG,2,AdvisoryRisk.SAFE,
                    DirectionDistribution(.7,.2,.1),.7,.7,.7,.7,.1)
                neutral=CandidateDecision("hype",Regime.TRENDING,Direction.NEUTRAL,0,AdvisoryRisk.SAFE,
                    DirectionDistribution(.1,.1,.8),.8,.7,.7,.7,.1)
                side_mismatch=CandidateDecision("pendle",Regime.TRENDING,Direction.LONG,2,AdvisoryRisk.SAFE,
                    DirectionDistribution(.6,.3,.1),.6,.7,.7,.7,.1)
                batch=DecisionBatch("request-cal",content_hash({}),release,"model-v1",100,90,now,
                    (judgment,neutral,side_mismatch),None,schema_hash="a"*64)
                batch_json=batch.to_json()
                ledger.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("decision-cal","snapshot-cal","request-cal",now,"model-v1",batch_json,
                     content_hash(__import__("json").loads(batch_json)),"completed",None,now,now))
                ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",("decision-cal",1,"hold","{}","[]",now))
                ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",("decision-cal",2,"rejected","{}","[\"veto\"]",now))
                ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",("decision-cal",3,"hold","{}","[]",now))
                ledger.append_forecast(forecast_id="forecast-cal",decision_id="decision-cal",candidate_id=1,
                    side="buy",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=now)
                with self.assertRaisesRegex(ContractViolation,"forecast.*before.*decision"):
                    ledger.append_forecast(forecast_id="backdated-forecast",decision_id="decision-cal",candidate_id=1,
                        side="sell",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=now-1)
                with self.assertRaisesRegex(PersistenceUnavailable,"calibrat.*verification"):
                    ledger.append_forecast(forecast_id="fabricated-calibration",decision_id="decision-cal",candidate_id=1,
                        side="buy",execution_policy_hash="d"*64,eligibility=False,recorded_at_us=now,
                        calibrated_p=.99,calibrator_hash="f"*64)
                ledger.append_forecast(forecast_id="forecast-neutral",decision_id="decision-cal",candidate_id=2,
                    side="sell",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=now)
                ledger.append_forecast(forecast_id="forecast-long-sell",decision_id="decision-cal",candidate_id=3,
                    side="sell",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=now)
                label=ledger.connection.execute("SELECT * FROM outcome_labels WHERE forecast_id='forecast-cal'").fetchone()
                self.assertEqual(("pending",None),(label["status"],label["outcome"]))
                ledger._clock_us=lambda:now+3
                first=ledger.revise_outcome_label(label["label_id"],"pending",{"missing_reason":"missing depth"},recorded_at_us=now+1)
                second=ledger.revise_outcome_label(label["label_id"],"pending",{"missing_reason":"missing fees"},recorded_at_us=now+2)
                history=ledger.connection.execute("SELECT label_event_id,previous_event_id FROM label_events WHERE label_id=? ORDER BY revision",(label["label_id"],)).fetchall()
                self.assertEqual((first,second),(history[1]["label_event_id"],history[2]["label_event_id"]))
                self.assertEqual(first,history[2]["previous_event_id"])
                with self.assertRaisesRegex(ContractViolation,"timestamp.*precedes"):
                    ledger.revise_outcome_label(label["label_id"],"pending",{"missing_reason":"backdated"},recorded_at_us=now+1)
                with self.assertRaisesRegex(ContractViolation,"pending.*outcome"):
                    ledger.revise_outcome_label(label["label_id"],"pending",{"missing_reason":"forged resolution","resolved_at_us":now+3},recorded_at_us=now+3)
                neutral_prob=__import__("json").loads(ledger.connection.execute(
                    "SELECT probabilities_json FROM forecasts WHERE forecast_id='forecast-neutral'").fetchone()[0])
                self.assertIsNone(neutral_prob["direction_probability"])
                self.assertEqual(.8,neutral_prob["direction_class_probability"])
                mismatched_prob=__import__("json").loads(ledger.connection.execute(
                    "SELECT probabilities_json FROM forecasts WHERE forecast_id='forecast-long-sell'").fetchone()[0])
                self.assertEqual(.3,mismatched_prob["direction_probability"])
                with self.assertRaises(PersistenceUnavailable):
                    ledger.revise_outcome_label(label["label_id"],"resolved",{
                        "net_positive_outcome":1,"terminal_direction_outcome":"long",
                        "net_return":"0.01","resolved_at_us":label["maturity_us"]+1,
                        "observed_through_us":label["maturity_us"],"execution_manifest_hash":"f"*64,
                        "costs_included":True,"observations":{"quote":True,"depth":True,
                            "fees":True,"slippage":True,"matured_data":True}},recorded_at_us=label["maturity_us"]+2)
                from bot.autonomous.calibration import (OutcomeEvidenceVerifier, VerifiedOutcomeEvidence)

                class SyntheticVerifier(OutcomeEvidenceVerifier):
                    missing=True
                    exit_price=Decimal("101")

                    def verify(self, request):
                        if self.missing:
                            return None
                        quote=request.forecast_at_us+100
                        closes=tuple((quote//HOUR_US+i)*HOUR_US for i in range(1,5))
                        return VerifiedOutcomeEvidence(self,request.request_hash,
                            quote,quote+1,quote+2,closes,closes[-1],closes[-1]+1,
                            closes[-1]+2,closes[-1]+3,Decimal("2"),Decimal("100"),self.exit_price,
                            Decimal(".5"),Decimal(".5"),Decimal(".25"),"f"*64,("e"*64,),None)

                verifier=SyntheticVerifier()
                ledger.configure_outcome_verifier(verifier)
                resolve_at=label["maturity_us"]+HOUR_US
                ledger._clock_us=lambda:resolve_at+10
                before=ledger.connection.execute("SELECT count(*) FROM label_events WHERE label_id=?",(label["label_id"],)).fetchone()[0]
                with self.assertRaises(PersistenceUnavailable):
                    ledger.revise_outcome_label(label["label_id"],"resolved",{"net_return":"999","source_refs":["untrusted"]},recorded_at_us=resolve_at)
                self.assertEqual(before,ledger.connection.execute("SELECT count(*) FROM label_events WHERE label_id=?",(label["label_id"],)).fetchone()[0])
                verifier.missing=False
                resolved_event=ledger.revise_outcome_label(label["label_id"],"resolved",{"net_return":"999"},recorded_at_us=resolve_at)
                resolved=ledger.connection.execute("SELECT * FROM outcome_labels WHERE label_id=?",(label["label_id"],)).fetchone()
                self.assertEqual(("resolved",1,"0.00375"),(resolved["status"],resolved["outcome"],resolved["net_return"]))
                verifier.exit_price=Decimal("99")
                corrected_event=ledger.revise_outcome_label(label["label_id"],"corrected",{"revision_ref":"new-source-revision"},recorded_at_us=resolve_at+1)
                corrected=ledger.connection.execute("SELECT * FROM outcome_labels WHERE label_id=?",(label["label_id"],)).fetchone()
                self.assertEqual(("corrected",0,"-0.01625"),(corrected["status"],corrected["outcome"],corrected["net_return"]))
                import json
                original=json.loads(ledger.connection.execute("SELECT payload_json FROM label_events WHERE label_event_id=?",(resolved_event,)).fetchone()[0])
                self.assertEqual(1,original["net_positive_outcome"])
                self.assertEqual(resolved_event,ledger.connection.execute("SELECT previous_event_id FROM label_events WHERE label_event_id=?",(corrected_event,)).fetchone()[0])
                ledger.connection.execute("UPDATE outcome_labels SET outcome=1 WHERE label_id=?",(label["label_id"],))
                path=Path(directory)/"ledger.sqlite3"
                ledger.close()
                with self.assertRaisesRegex(PersistenceUnavailable,"label projection does not match event history"):
                    Ledger.open(path)
                return
            finally: ledger.close()


    def test_resolved_ledger_label_scores_brier_without_approval(self):
        from bot.autonomous.worker import PaperWorker
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger.open(Path(directory) / "ledger.sqlite3")
            try:
                ledger.create_paper_account("paper-calibration")
                now = 20_000_000_000
                ledger._clock_us = lambda: now
                release = content_hash({})
                ledger.connection.execute("INSERT INTO release_bundles VALUES (?,?,NULL,?,?)", (release, "{}", now + 100_000_000, now))
                close = now - 1000
                ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("snapshot-cal", "paper-calibration", release, close, close, "b" * 64, "{}", "d" * 64, 0, "c" * 64, "{}", 1, None, now))
                judgment = CandidateDecision("aave", Regime.TRENDING, Direction.LONG, 2, AdvisoryRisk.SAFE,
                    DirectionDistribution(.7, .2, .1), .7, .7, .7, .7, .1)
                batch = DecisionBatch("request-cal", content_hash({}), release, "model-v1", 100, 90, now,
                    (judgment,), None, schema_hash="a" * 64)
                batch_json = batch.to_json()
                ledger.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("decision-cal", "snapshot-cal", "request-cal", now, "model-v1", batch_json,
                     content_hash(__import__("json").loads(batch_json)), "completed", None, now, now))
                ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",
                    ("decision-cal", 1, "hold", "{}", "[]", now))
                ledger.append_forecast(forecast_id="forecast-cal", decision_id="decision-cal", candidate_id=1,
                    side="buy", execution_policy_hash="e" * 64, eligibility=False, recorded_at_us=now)
                label = ledger.connection.execute("SELECT * FROM outcome_labels WHERE forecast_id='forecast-cal'").fetchone()

                class SyntheticVerifier(OutcomeEvidenceVerifier):
                    def verify(self, request):
                        quote = request.forecast_at_us + 100
                        closes = tuple((quote // HOUR_US + i) * HOUR_US for i in range(1, 5))
                        return VerifiedOutcomeEvidence(self, request.request_hash, quote, quote + 1, quote + 2,
                            closes, closes[-1], closes[-1] + 1, closes[-1] + 2, closes[-1] + 3,
                            Decimal("2"), Decimal("100"), Decimal("101"), Decimal(".5"), Decimal(".5"),
                            Decimal(".25"), "f" * 64, ("e" * 64,), None)

                ledger.configure_outcome_verifier(SyntheticVerifier())
                resolve_at = label["maturity_us"] + HOUR_US
                ledger._clock_us = lambda: resolve_at + 10
                ledger.revise_outcome_label(label["label_id"], "resolved", {"net_return": "999"}, recorded_at_us=resolve_at)
                ledger.connection.execute(
                    "INSERT INTO order_intents VALUES (?,?,?,1,?,?,?,'AAVE-USD','buy','1','100',1000,?,'filled',NULL,0,0,0,?,?,?)",
                    ("intent-cal", "paper-calibration", "decision-cal", release, close, "entry", "client-cal",
                     "{}", "{}", resolve_at))
                ledger.connection.execute(
                    "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("fill-cal", "paper-calibration", "intent-cal", "paper", "trade-cal", "order-cal", "AAVE-USD",
                     "buy", "1", "100", "100", "USD", "0.1", close, close, "a" * 64, resolve_at))
                from bot.autonomous.brain import BrainProposal

                class ProposingBrain:
                    model_id = "test-brain"

                    def review(self, *, job_id, archive_hash, metrics):
                        return BrainProposal("propose_schema", "brier recorded", "a human still has to accept it",
                            (archive_hash,), schema_diff="research-only", model_id=self.model_id)

                worker = PaperWorker(SimpleNamespace(venue=SimpleNamespace(venue="paper")))
                worker.review_brain = ProposingBrain()
                worker.attach_activity(ledger.connection, "paper-calibration", "a" * 64)
                result = worker.on_clock(resolve_at)
                self.assertTrue(result["reviewed"])
                self.assertFalse(result["dispatched"])
                report = worker.activity_reports[0]
                self.assertFalse(report.approved)
                self.assertEqual(report.status, "pending_agenkit")
                self.assertEqual(report.schema_diff, "research-only")
                self.assertEqual(report.resolved, 1)
                self.assertEqual(report.included_ids, ("fill-cal",))
                self.assertAlmostEqual(report.binary_brier, (0.7 - 1) ** 2)
            finally:
                ledger.close()


class EdgeGateTests(unittest.TestCase):
    """Attestation from ledger label events plus a ledger-verified decision are both required for an edge."""
    _observations = CalibrationTests._observations
    START = 1_000 * HOUR_US
    BLOCK = 100

    # Pure attestation checks over supplied rows.

    def inputs(self, *, calibration=None, expires_after_us=1000*HOUR_US):
        training=self._observations("train",400,start_us=1_000_000_000_000)
        train_cutoff=max(r["resolved_at_us"] for r in training)+1
        calibration=calibration or self._observations("cal",400,start_us=train_cutoff+HOUR_US)
        calibration_cutoff=max(r["resolved_at_us"] for r in calibration)+1
        kwargs=dict(model_id="model-v1",schema_hash="a"*64,dictionary_hash="b"*64,
            label_hash="c"*64,cost_hash="d"*64,instrument="AAVE-USD",regime=Regime.TRENDING,
            training_cutoff_us=train_cutoff,calibration_cutoff_us=calibration_cutoff,
            valid_from_us=calibration_cutoff+1,expires_at_us=calibration_cutoff+expires_after_us)
        for row in (*training,*calibration):row["instrument"]="AAVE-USD"
        return training,calibration,kwargs

    def test_attestation_rebuilds_exact_artifact_and_sets_both_checks(self):
        training,calibration,kwargs=self.inputs()
        base=build_calibration_artifact(training=training,calibration=calibration,**kwargs)
        attested,metrics=attest_calibration_artifact(base,training=training,calibration=calibration,**kwargs)
        self.assertTrue(attested.uncertainty_passed and attested.tail_checks_passed)
        self.assertNotEqual(base.artifact_hash,attested.artifact_hash)
        self.assertEqual(base.artifact_hash,metrics["base_artifact_hash"])
        forged=replace(base,win_per_dollar=Decimal("0.5"))
        with self.assertRaisesRegex(ValueError,"rebuild"):
            attest_calibration_artifact(forged,training=training,calibration=calibration,**kwargs)

    def test_out_of_sample_loss_beyond_training_tail_rejects(self):
        training,calibration,kwargs=self.inputs()
        calibration[5]["net_return"]=Decimal("-0.5");calibration[5]["net_positive_outcome"]=0
        base=build_calibration_artifact(training=training,calibration=calibration,**kwargs)
        with self.assertRaisesRegex(ValueError,"tail"):
            attest_calibration_artifact(base,training=training,calibration=calibration,**kwargs)

    def test_bin_rate_unstable_across_chronological_halves_rejects(self):
        training,calibration,kwargs=self.inputs()
        for row in calibration[200:]:
            row["net_positive_outcome"]=0;row["net_return"]=Decimal("-0.02")
        base=build_calibration_artifact(training=training,calibration=calibration,**kwargs)
        with self.assertRaisesRegex(ValueError,"uncertainty"):
            attest_calibration_artifact(base,training=training,calibration=calibration,**kwargs)

    def test_single_sample_half_cannot_pass_uncertainty(self):
        training,_,kwargs=self.inputs()
        rows=self._observations("cal",300,start_us=kwargs["training_cutoff_us"]+HOUR_US,probabilities=(.05,))
        for row in rows:row["instrument"]="AAVE-USD"
        # Top bin: 100 early rows and a single winning late row. Bottom bin straddles both halves.
        for row in rows[:100]+[rows[299]]:row["direction_probability"]=.95
        rows[299]["net_positive_outcome"]=1;rows[299]["net_return"]=Decimal("0.01")
        cutoff=max(r["resolved_at_us"] for r in rows)+1
        args=dict(kwargs,calibration_cutoff_us=cutoff,valid_from_us=cutoff+1,expires_at_us=cutoff+HOUR_US)
        base=build_calibration_artifact(training=training,calibration=rows,**args)
        self.assertEqual(101,next(b for b in base.mapping if b.upper==1.0).independent_samples)
        with self.assertRaisesRegex(ValueError,"uncertainty"):
            attest_calibration_artifact(base,training=training,calibration=rows,**args)

    def test_too_few_training_tail_outcomes_rejects(self):
        training,calibration,kwargs=self.inputs()
        losers=[row for row in training if row["net_positive_outcome"]==0]
        for row in losers[10:]:
            row["net_positive_outcome"]=1;row["net_return"]=Decimal("0.01")
        base=build_calibration_artifact(training=training,calibration=calibration,**kwargs)
        with self.assertRaisesRegex(ValueError,"tail"):
            attest_calibration_artifact(base,training=training,calibration=calibration,**kwargs)

    # Ledger-backed calibration and edge gate.

    class Verifier(OutcomeEvidenceVerifier):
        def __init__(self):self.exit_prices={}
        def verify(self, request):
            quote=request.forecast_at_us+100
            closes=tuple((quote//HOUR_US+i)*HOUR_US for i in range(1,5))
            return VerifiedOutcomeEvidence(self,request.request_hash,quote,quote+1,quote+2,closes,closes[-1],
                closes[-1]+1,closes[-1]+2,closes[-1]+3,Decimal("1"),Decimal("100"),
                self.exit_prices[request.forecast_id],Decimal("0"),Decimal("0"),Decimal("0"),"f"*64,("e"*64,),
                "d"*64 if request.side=="sell" else None)

    def _decision(self, ledger, name, release, close, state, decisions, *, model="model-v1"):
        state_json=canonical_bytes(state).decode()
        ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("snapshot-"+name,"paper",release,close,close,"b"*64,state_json,"d"*64,0,"c"*64,"{}",1,None,close+1))
        batch=DecisionBatch("request-"+name,content_hash(state),release,model,100,90,close+2,decisions,None,schema_hash="a"*64)
        batch_json=batch.to_json()
        ledger.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("decision-"+name,"snapshot-"+name,"request-"+name,close+2,model,batch_json,
             content_hash(strict_json(batch_json)),"completed",None,close+2,close+2))
        return batch

    def _label_history(self, ledger, release, verifier, prefix, start, win_exit=Decimal("104"), noise=False):
        judgment=CandidateDecision("aave",Regime.TRENDING,Direction.LONG,2,AdvisoryRisk.SAFE,
            DirectionDistribution(.7,.2,.1),.7,.7,.7,.7,.1)
        for i in range(self.BLOCK):
            close=start+i*5*HOUR_US;name=f"{prefix}-{i}"
            self._decision(ledger,name,release,close,{},(judgment,))
            ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",("decision-"+name,1,"hold","{}","[]",close+3))
            ledger.append_forecast(forecast_id="forecast-"+name,decision_id="decision-"+name,candidate_id=1,
                side="buy",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=close+10)
            verifier.exit_prices["forecast-"+name]=win_exit if i%10<7 else Decimal("98")
            forecasts=["forecast-"+name]
            if noise and i%10==3:
                # A sell counterfactual on the same decision: other side, same window.
                ledger.append_forecast(forecast_id="sell-"+name,decision_id="decision-"+name,candidate_id=1,
                    side="sell",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=close+11)
                verifier.exit_prices["sell-"+name]=Decimal("98");forecasts.append("sell-"+name)
            if noise and i%10==5:
                # A second AAVE buy one hour later overlaps this window and must be purged.
                late=close+HOUR_US;late_name=name+"-late"
                self._decision(ledger,late_name,release,late,{},(judgment,))
                ledger.connection.execute("INSERT INTO candidate_results VALUES (?,?,?,?,?,?)",("decision-"+late_name,1,"hold","{}","[]",late+3))
                ledger.append_forecast(forecast_id="forecast-"+late_name,decision_id="decision-"+late_name,candidate_id=1,
                    side="buy",execution_policy_hash="e"*64,eligibility=False,recorded_at_us=late+10)
                verifier.exit_prices["forecast-"+late_name]=Decimal("98");forecasts.append("forecast-"+late_name)
            for forecast in forecasts:
                label=ledger.connection.execute("SELECT label_id,maturity_us FROM outcome_labels WHERE forecast_id=?",(forecast,)).fetchone()
                self._at(ledger,label["maturity_us"]+HOUR_US)
                ledger.revise_outcome_label(label["label_id"],"resolved",{},recorded_at_us=label["maturity_us"]+HOUR_US)
        return start+self.BLOCK*5*HOUR_US+2*HOUR_US

    @staticmethod
    def _at(ledger, now):
        ledger._clock_us=lambda:now

    def build_args(self, training_cutoff, calibration_cutoff, *, expires_after_us=1000*HOUR_US):
        from bot.autonomous.calibration import LABEL_DEFINITION
        return dict(model_id="model-v1",schema_hash="a"*64,dictionary_hash="b"*64,
            label_hash=content_hash(LABEL_DEFINITION),cost_hash="e"*64,instrument="AAVE-USD",regime=Regime.TRENDING,
            training_cutoff_us=training_cutoff,calibration_cutoff_us=calibration_cutoff,
            valid_from_us=calibration_cutoff+1,expires_at_us=calibration_cutoff+expires_after_us)

    def ledger_fixture(self, directory, *, calibrator_hash=None, record=True, live_model="model-v1", expires_after_us=1000*HOUR_US,
                       live_direction=Direction.LONG, win_exit=Decimal("104"), noise=False):
        """Real ledger: 100 training + 100 calibration verified labels, then one live persisted decision."""
        ledger=Ledger.open(Path(directory)/"ledger.sqlite3")
        ledger.create_paper_account("paper")
        verifier=self.Verifier();ledger.configure_outcome_verifier(verifier)
        history=content_hash({"history":1})
        ledger.connection.execute("INSERT INTO release_bundles VALUES (?,?,NULL,?,?)",(history,"{}",10**15,0))
        training_cutoff=self._label_history(ledger,history,verifier,"train",self.START,win_exit,noise)
        calibration_cutoff=self._label_history(ledger,history,verifier,"cal",training_cutoff+HOUR_US,win_exit,noise)
        args=self.build_args(training_cutoff,calibration_cutoff,expires_after_us=expires_after_us)
        training=ledger.calibration_rows(before_us=training_cutoff,**self.cohort(args))
        calibration=ledger.calibration_rows(before_us=calibration_cutoff,after_us=training_cutoff,**self.cohort(args))
        attested,_=attest_calibration_artifact(build_calibration_artifact(training=training,calibration=calibration,**args),
            training=training,calibration=calibration,**args)
        close=calibration_cutoff+10*HOUR_US
        now=close+1000;ledger._clock_us=lambda:now
        from bot.autonomous.contracts import ApprovedBundle, CandidateBundle, CandidateDefinition, GateApproval
        candidates=tuple(CandidateDefinition(c,c.upper()+"-USD",c=="aave",False,(),"e"*64 if c=="aave" else None)
            for c in ("aave","hype","pendle"))
        bundle=CandidateBundle("model-v1","a"*64,"b"*64,"b"*64,"c"*64,"d"*64,calibrator_hash or attested.artifact_hash,
            "f"*64,"1"*64,now+100*HOUR_US,candidates,())
        self.bundle=ApprovedBundle(bundle,(GateApproval("evaluation","test",bundle.bundle_hash,"2"*64,1),),now-1,"3"*64)
        # Release identity here is bundle_hash; reconciling it with the ledger's core-manifest hash is Task 15.
        release=bundle.bundle_hash
        ledger.connection.execute("INSERT INTO release_bundles VALUES (?,?,NULL,?,?)",
            (release,canonical_bytes(bundle.to_dict()).decode(),now+100*HOUR_US,now))
        if record:
            ledger.record_calibration_attestation(release_hash=release,**args)
        snapshots={c:self._snapshot(c,release,close) for c in ("aave","hype","pendle")}
        if live_direction is Direction.LONG:
            decision=CandidateDecision("aave",Regime.TRENDING,Direction.LONG,2,AdvisoryRisk.SAFE,
                DirectionDistribution(.7,.2,.1),.85,.85,.85,.85,.1)
        else:
            decision=CandidateDecision("aave",Regime.TRENDING,Direction.SHORT,2,AdvisoryRisk.SAFE,
                DirectionDistribution(.2,.7,.1),.85,.85,.85,.85,.1)
        foreign=replace(decision,candidate_id="hype")
        neutral=CandidateDecision("pendle",Regime.TRENDING,Direction.NEUTRAL,0,AdvisoryRisk.SAFE,
            DirectionDistribution(.1,.1,.8),.8,.7,.7,.7,.1)
        state=[[1]+[0]*16,[self._wire(snapshots[c]) for c in ("aave","hype","pendle")]]
        batch=self._decision(ledger,"live",release,close,state,(decision,foreign,neutral),model=live_model)
        for c in ("hype","pendle"):
            ledger.connection.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("snapshot-live-"+c,"paper",release,close,close,"b"*64,canonical_bytes(state).decode(),"d"*64,0,"c"*64,"{}",1,None,close+1))
        self.now=now;self.args=args;self.release=release;self.foreign=foreign
        self.others={c:replace(snapshots[c],snapshot_id="snapshot-live-"+c) for c in ("hype","pendle")}
        return ledger,attested,batch,decision,neutral,replace(snapshots["aave"],snapshot_id="snapshot-live")

    @staticmethod
    def _snapshot(candidate, release, close):
        from bot.autonomous.contracts import default_field_dictionary
        fields=default_field_dictionary().candidate_fields
        features=tuple(Feature(f.name,1.0 if f.name=="mid" else 0.0,f.name=="mid") for f in fields)
        mask=1<<next(i for i,f in enumerate(fields) if f.name=="mid")
        return NumericSnapshot(candidate,candidate.upper()+"-USD","e"*64,CausalCutoffs(close,close,1,"boot"),
            features,mask,"c"*64,AccountRevision(),account_id="paper",release_hash=release)

    @staticmethod
    def _wire(snapshot):
        from bot.autonomous.state import _wire_values
        from bot.autonomous.contracts import default_field_dictionary
        return _wire_values(snapshot.features,default_field_dictionary().candidate_fields,snapshot.candidate_id)

    @staticmethod
    def cohort(args):
        keys=("instrument","regime","model_id","schema_hash","dictionary_hash","label_hash","cost_hash")
        return dict({k:args[k] for k in keys},side="buy")

    def edge(self, ledger, *args, **kw):
        kw.setdefault("now_us",self.now)
        return estimate_edge(*args,ledger=ledger,**kw)

    def test_verified_decision_and_recorded_attestation_yield_conservative_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory)
            try:
                result=self.edge(ledger,batch,decision,snapshot,artifact)
                self.assertIsInstance(result,EdgeEstimate,getattr(result,"reason_code",None))
                self.assertEqual(wilson_lower(70,100),result.calibrated_probability)
                self.assertEqual((Decimal("0.04"),Decimal("0.02"),artifact.artifact_hash),
                    (result.win_per_dollar,result.loss_per_dollar,result.artifact_hash))
                self.assertEqual(snapshot.cutoffs.candle_close_us+HOUR_US,result.valid_until_us)
            finally:ledger.close()

    def test_each_gate_failure_has_its_own_reason(self):
        class FakeLedger:
            def verified_decision_snapshot(self,*a):return {"field_hash":"b"*64}
            def attested_calibration(self,*a):return artifact
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,neutral,snapshot=self.ledger_fixture(directory)
            try:
                other_features=tuple(replace(f,value=2.0) if f.name=="mid" else f for f in snapshot.features)
                cases={
                    "neutral_direction_has_no_entry_edge":((batch,neutral,self.others["pendle"],artifact),{}),
                    "unsupported_cohort":((batch,self.foreign,self.others["hype"],artifact),{}),
                    "calibration_attestation_unrecorded":((batch,decision,snapshot,replace(artifact,artifact_hash="9"*64)),{}),
                    "artifact_evidence_incomplete":((batch,decision,snapshot,replace(artifact,uncertainty_passed=False)),{}),
                    "persisted_decision_verification_failed":((replace(batch,model_id="model-v2"),decision,snapshot,artifact),{}),
                    "decision_not_in_persisted_batch":((batch,replace(decision,setup_quality=3),snapshot,artifact),{}),
                    "edge_evaluated_outside_validity":((batch,decision,snapshot,artifact),{"now_us":snapshot.cutoffs.candle_close_us+HOUR_US}),
                    "evaluation_time_required":((batch,decision,snapshot,artifact),{"now_us":None}),
                }
                for reason,(args,kw) in cases.items():
                    with self.subTest(reason):
                        result=self.edge(ledger,*args,**kw)
                        self.assertIsInstance(result,UnavailableEdge)
                        self.assertEqual(reason,result.reason_code)
                # Persisted snapshot binding: the candidate row and instrument must match the judged state.
                for name,bad in {"features":replace(snapshot,features=other_features),
                        "candidate_swap":replace(snapshot,candidate_id="hype"),
                        "instrument":replace(snapshot,instrument="ETH-USD")}.items():
                    with self.subTest(name):
                        judged=self.foreign if name=="candidate_swap" else decision
                        self.assertEqual("persisted_decision_verification_failed",
                            self.edge(ledger,batch,judged,bad,artifact).reason_code)
                self.assertEqual("persisted_decision_verification_required",
                    estimate_edge(batch,decision,snapshot,artifact,ledger=FakeLedger(),now_us=self.now).reason_code)
            finally:ledger.close()

    def test_version_and_window_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory,live_model="model-v2")
            try:self.assertEqual("artifact_version_mismatch",self.edge(ledger,batch,decision,snapshot,artifact).reason_code)
            finally:ledger.close()
        with tempfile.TemporaryDirectory() as directory:
            # Artifact expires exactly at the live candle close.
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory,expires_after_us=10*HOUR_US)
            try:self.assertEqual("artifact_outside_validity_window",self.edge(ledger,batch,decision,snapshot,artifact).reason_code)
            finally:ledger.close()

    def test_short_forecast_has_no_edge_from_long_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory,live_direction=Direction.SHORT)
            try:
                self.assertEqual(("buy",),artifact.sides)
                self.assertEqual("unsupported_side",self.edge(ledger,batch,decision,snapshot,artifact).reason_code)
            finally:ledger.close()

    def test_cross_cohort_side_and_overlapping_history_is_filtered_and_purged(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory,noise=True)
            try:
                rows=ledger.calibration_rows(before_us=self.args["training_cutoff_us"],**self.cohort(self.args))
                self.assertEqual(self.BLOCK,len(rows))
                self.assertFalse(any(r["forecast_id"].startswith("sell-") or r["forecast_id"].endswith("-late") for r in rows))
                self.assertIsInstance(self.edge(ledger,batch,decision,snapshot,artifact),EdgeEstimate)
            finally:ledger.close()

    def test_release_must_name_the_attested_calibrator(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContractViolation,"calibrator"):
                self.ledger_fixture(directory,calibrator_hash="7"*64)

    def test_unrecorded_attestation_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory,record=False)
            try:
                self.assertEqual("calibration_attestation_unrecorded",self.edge(ledger,batch,decision,snapshot,artifact).reason_code)
            finally:ledger.close()

    def test_rows_come_from_ledger_as_of_cutoff_and_future_cutoffs_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger,artifact,batch,decision,_,snapshot=self.ledger_fixture(directory)
            try:
                cutoff=self.args["calibration_cutoff_us"]
                before=ledger.calibration_rows(before_us=cutoff,after_us=self.args["training_cutoff_us"],**self.cohort(self.args))
                self.assertEqual(self.BLOCK,len(before))
                # A correction recorded after the cutoff is invisible to that block.
                label=ledger.connection.execute("SELECT label_id FROM outcome_labels WHERE forecast_id='forecast-cal-0'").fetchone()[0]
                ledger._outcome_verifier.exit_prices["forecast-cal-0"]=Decimal("98")
                ledger.revise_outcome_label(label,"corrected",{},recorded_at_us=self.now)
                self.assertEqual(before,ledger.calibration_rows(before_us=cutoff,after_us=self.args["training_cutoff_us"],**self.cohort(self.args)))
                # Label events cannot be backdated before the latest one, nor into an attested block.
                with self.assertRaisesRegex(ContractViolation,"precedes"):
                    ledger.revise_outcome_label(label,"corrected",{},recorded_at_us=cutoff)
                with self.assertRaisesRegex(ContractViolation,"future"):
                    ledger.record_calibration_attestation(release_hash=self.release,
                        **dict(self.args,calibration_cutoff_us=self.now+1,valid_from_us=self.now+2,expires_at_us=self.now+3))
                # The same calibrator can be attested for a second release.
                manifest={"calibrator_hash":artifact.artifact_hash,"schema_hash":"a"*64,"policy":"v2"}
                second=content_hash(manifest)
                ledger.connection.execute("INSERT INTO release_bundles VALUES (?,?,NULL,?,?)",
                    (second,canonical_bytes(manifest).decode(),self.now+100*HOUR_US,self.now))
                self.assertEqual(artifact.artifact_hash,ledger.record_calibration_attestation(release_hash=second,**self.args))
                self.assertEqual(artifact,ledger.attested_calibration(artifact.artifact_hash,second))
            finally:ledger.close()


if __name__ == "__main__": unittest.main()
