"""Tests for cost accounting: cache-aware tokens and cross-process state."""

from lup.client import CostAccumulator, StageCost, input_tokens_from_usage


class TestInputTokenAccounting:
    def test_cache_tokens_count_as_input(self) -> None:
        usage = {
            "input_tokens": 108,
            "cache_creation_input_tokens": 40_000,
            "cache_read_input_tokens": 900_000,
            "output_tokens": 52_933,
        }
        assert input_tokens_from_usage(usage) == 108 + 40_000 + 900_000

    def test_non_int_values_are_ignored(self) -> None:
        usage = {"input_tokens": 10, "cache_read_input_tokens": {"nested": 1}}
        assert input_tokens_from_usage(usage) == 10


class TestCostStateRoundTrip:
    def seeded(self) -> CostAccumulator:
        acc = CostAccumulator()
        acc.total_cost_usd = 2.95
        acc.total_duration_ms = 1_056_644.0
        acc.total_input_tokens = 940_108
        acc.total_output_tokens = 52_933
        acc.call_count = 3
        return acc

    def test_resume_carries_prior_process_costs(self) -> None:
        first_process = self.seeded()
        state = first_process.state_dict()

        second_process = CostAccumulator()
        second_process.load_state(state)
        second_process.total_cost_usd += 1.05
        second_process.call_count += 1

        assert second_process.total_cost_usd == 2.95 + 1.05
        assert second_process.call_count == 4
        assert second_process.total_input_tokens == 940_108

    def test_repeated_resume_does_not_double_count(self) -> None:
        first = self.seeded()
        resumed = CostAccumulator()
        resumed.load_state(first.state_dict())

        third = CostAccumulator()
        third.load_state(resumed.state_dict())

        assert third.total_cost_usd == first.total_cost_usd
        assert third.call_count == first.call_count

    def test_stage_breakdown_survives_round_trip(self) -> None:
        acc = self.seeded()
        acc.stages["rewrite"] = StageCost()
        acc.stages["rewrite"].cost_usd = 2.95
        acc.stages["rewrite"].call_count = 1

        restored = CostAccumulator()
        restored.load_state(acc.state_dict())

        assert restored.stages["rewrite"].cost_usd == 2.95
        assert restored.stages["rewrite"].call_count == 1
