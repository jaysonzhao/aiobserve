from app.scale_gate import DEFAULT_POLICY, build_gate_queries, evaluate_workload


def scope():
    return {
        "namespace": "llm", "model_name": "qwen", "deployment": "qwen-serving",
        "serving_label": None, "serving_value": None,
        "pod_names": ["qwen-serving-abcde"], "job_values": [], "instance_values": [],
        "engine_values": [], "source_values": [],
    }


def capture_inputs(pods, times, overrides=None):
    overrides = overrides or {}
    names = (
        "ttft", "ttft_observations", "ttft_age", "tpot", "tpot_observations", "tpot_age",
        "waiting", "waiting_age", "kv", "kv_age",
    )
    defaults = {
        "ttft": 4.0, "ttft_observations": 40, "ttft_age": 0,
        "tpot": 0.1, "tpot_observations": 40, "tpot_age": 0,
        "waiting": 0, "waiting_age": 0, "kv": 0.5, "kv_age": 0,
    }
    inputs = {}
    for name in names:
        series = []
        for pod in pods:
            values = []
            for timestamp in times:
                value = overrides.get((pod, name, timestamp), defaults[name])
                if value is not None:
                    values.append({"timestamp": timestamp, "value": value, "raw": str(value)})
            series.append({"labels": {"pod": pod}, "deployment": "qwen-serving", "model_name": "qwen", "points": values})
        inputs[name] = {"state": "valid", "reason": None, "query": "fixture", "series": series}
    return inputs


def evaluate(inputs, pods=None, end=600):
    return evaluate_workload(
        deployment="qwen-serving", model_name="qwen", expected_pods=pods or ["qwen-serving-abcde"],
        inputs=inputs, policy=dict(DEFAULT_POLICY), start=0, end=end,
    )


def test_gate_queries_use_fixed_p95_and_group_engines_within_each_pod():
    queries = build_gate_queries(scope(), dict(DEFAULT_POLICY), {})
    assert "histogram_quantile(0.95" in queries["ttft"]
    assert "sum by (le, namespace, model_name, pod)" in queries["tpot"]
    assert "request_time_per_output_token_seconds_bucket" in queries["tpot"]
    assert "avg_over_time(vllm:num_requests_waiting" in queries["waiting"]
    assert "max by (namespace, model_name, pod) (vllm:kv_cache_usage_perc" in queries["kv"]
    assert "instance" not in queries["ttft"]


def test_strict_thresholds_and_or_branches_confirm_after_continuous_hold():
    times = list(range(0, 601, 30))
    inputs = capture_inputs(["qwen-serving-abcde"], times)
    for timestamp in times:
        inputs["tpot"]["series"][0]["points"][timestamp // 30]["value"] = 0.2
        inputs["kv"]["series"][0]["points"][timestamp // 30]["value"] = 0.95
    result = evaluate(inputs)
    rows = result["pods"][0]["evaluations"]
    assert rows[0]["state"] == "review_pending"
    assert rows[10]["state"] == "scale_out_review"
    assert rows[10]["confirmed_at"] == 300

    equal = capture_inputs(["qwen-serving-abcde"], times)
    for timestamp in times:
        i = timestamp // 30
        equal["ttft"]["series"][0]["points"][i]["value"] = 3.0
        equal["tpot"]["series"][0]["points"][i]["value"] = 0.15
        equal["waiting"]["series"][0]["points"][i]["value"] = 5
        equal["kv"]["series"][0]["points"][i]["value"] = 0.9
    assert evaluate(equal)["pods"][0]["latest"]["state"] == "within_targets"


def test_latency_and_pressure_on_different_pods_do_not_combine():
    pods = ["pod-a", "pod-b"]
    times = list(range(0, 601, 30))
    inputs = capture_inputs(pods, times)
    for timestamp in times:
        index = timestamp // 30
        inputs["ttft"]["series"][0]["points"][index]["value"] = 1
        inputs["waiting"]["series"][0]["points"][index]["value"] = 6
        inputs["waiting"]["series"][1]["points"][index]["value"] = 0
    result = evaluate(inputs, pods)
    assert result["pods"][0]["latest"]["state"] == "pressure_warning"
    assert result["pods"][1]["latest"]["state"] == "latency_investigation"
    assert all(row["state"] != "scale_out_review" for pod in result["pods"] for row in pod["evaluations"])


def test_missing_histogram_observations_reset_candidate_hold():
    times = list(range(0, 601, 30))
    inputs = capture_inputs(["qwen-serving-abcde"], times)
    for timestamp in times:
        inputs["kv"]["series"][0]["points"][timestamp // 30]["value"] = 0.95
    inputs["ttft_observations"]["series"][0]["points"][2]["value"] = 0
    rows = evaluate(inputs)["pods"][0]["evaluations"]
    assert rows[1]["candidate_start"] == 0
    assert rows[2]["state"] == "insufficient_data"
    assert rows[3]["candidate_start"] == 90
    assert rows[13]["confirmed_at"] == 390


def test_confirmation_time_is_the_first_evaluation_after_hold():
    times = list(range(0, 361, 60))
    inputs = capture_inputs(["qwen-serving-abcde"], times)
    for timestamp in times:
        inputs["kv"]["series"][0]["points"][timestamp // 60]["value"] = 0.95
    policy = dict(DEFAULT_POLICY, evaluation_step_seconds=60, hold_seconds=90)
    result = evaluate_workload(
        deployment="qwen-serving", model_name="qwen", expected_pods=["qwen-serving-abcde"],
        inputs=inputs, policy=policy, start=0, end=360,
    )
    assert result["pods"][0]["evaluations"][2]["timestamp"] == 120
    assert result["pods"][0]["evaluations"][2]["confirmed_at"] == 120
