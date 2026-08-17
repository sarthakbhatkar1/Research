async def debug_cache(client_id=None, show_values: bool = False):
    """Inspect routing state: active strategy configs + per-deployment metrics.

    Query params:
        client_id: Optional raw client ID to filter scoped keys.
        show_values: Include raw cache values in addition to the routing_summary.

    Key patterns by routing strategy:
        latency-based-routing:     {model_group}_map
        cost-based-routing:        {model_group}_map
        usage-based-routing-v2:    {id}:{deployment}:tpm:{minute} / :rpm:{minute}
        usage-based-routing:       {model_group}:tpm:{minute} / :rpm:{minute}
        least-busy:                {model_group}_request_count
        cooldowns (all strategies): deployment:{id}:cooldown

    Every "deployments" entry below is: identity fields (deployment_id, model,
    api_base, timeout_configured_s) + cooldown fields (in_cooldown,
    cooldown_expires_in_s, cooldown_reason) + a strategy-specific "metrics" dict.
    There is deliberately no "currently_preferred" flag: litellm's actual
    selection is weighted/randomized within routing_strategy_args (e.g.
    lowest_latency_buffer) and additionally excludes deployments in cooldown, so
    a simple min()/max() over the metrics shown here would not reliably match
    what the router actually picks next. To confirm a strategy is influencing
    real traffic, correlate the `x-litellm-model-id` response header across a
    batch of real calls against this snapshot, rather than trusting a
    "preferred" label computed here.
    """
    import time
    from datetime import datetime as _dt

    router = getattr(litellm.proxy.proxy_server, "llm_router", None)
    logger.info(f"[CUSTOM_CACHING] router: {router}")
    if not (router and getattr(router, "cache", None)):
        return {"error": "no router or cache not initialized"}

    dual = router.cache
    logger.info(f"[CUSTOM_CACHING] dual cache: {dual}")
    mem = getattr(dual, "in_memory_cache", None)
    result: dict = {"router_cache_type": type(dual).__name__}
    prefix = f"cid:{client_id}:" if client_id else None

    def _is_map_key(k: str) -> bool:
        return k.endswith("_map") or k.endswith("_request_count")

    def _is_counter_key(k: str) -> bool:
        return ":tpm:" in k or ":rpm:" in k

    def _is_cooldown_key(k: str) -> bool:
        return k.startswith("deployment:") and k.endswith(":cooldown")

    cache_dict: dict = getattr(mem, "cache_dict", {}) if mem else {}

    # --- L1: InMemoryCache ---
    # NOTE: cache_dict is the LIVE dict backing every routing decision in this
    # process; it has no lock and is actively evicted on every set_cache() call
    # (max_size_in_memory defaults to 200). Snapshot keys first, then use .get()
    # with a sentinel for the value lookups below so a key evicted between the
    # snapshot and the lookup doesn't raise KeyError.
    if mem:
        all_keys = list(cache_dict.keys())
        display_keys = [k for k in all_keys if k.startswith(prefix)] if prefix else all_keys
        result["in_memory_size_total"] = len(all_keys)
        result["in_memory_size_filtered"] = len(display_keys)
        result["in_memory_keys"] = display_keys[:50]
        if show_values:
            _MISSING = object()
            map_vals: dict = {}
            counter_vals: dict = {}
            cooldown_vals: dict = {}
            for k in display_keys:
                v = cache_dict.get(k, _MISSING)
                if v is _MISSING:
                    # evicted between the snapshot above and this lookup, skip it
                    continue
                if _is_cooldown_key(k):
                    cooldown_vals[k] = v
                elif _is_map_key(k):
                    map_vals[k] = v
                elif _is_counter_key(k):
                    counter_vals[k] = v
            if map_vals:
                result["in_memory_map_values"] = map_vals
            if counter_vals:
                result["in_memory_counter_values"] = counter_vals
            if cooldown_vals:
                result["in_memory_cooldown_values"] = cooldown_vals
    else:
        result["in_memory_keys"] = []
        result["in_memory_size_total"] = 0

    # --- L2: PostgresCache (actual DB query) ---
    # Always decoded regardless of show_values so routing_groups can fall back to L2 on cold L1
    l2_map_entries: dict = {}
    l2_counter_entries: dict = {}
    l2_cooldown_entries: dict = {}
    l2 = getattr(dual, "postgres_cache", None)
    if l2:
        result["l2_cache_type"] = type(l2).__name__
        result["l2_initialized"] = getattr(l2, "_initialized", False)
        if getattr(l2, "_initialized", False):
            try:
                from common_svc.db.base import DBSession
                from sqlalchemy import or_
                _model = l2._model
                with DBSession() as db_session:
                    session = db_session.session
                    query = session.query(_model.state_key, _model.state_value, _model.expires_at)
                    if prefix:
                        query = query.filter(_model.state_key.like(f"{prefix}%"))
                    else:
                        # _map: latency/cost; _request_count: least-busy;
                        # :tpm:/:rpm:: usage-based; deployment:*:cooldown: cooldowns
                        query = query.filter(or_(
                            _model.state_key.like("%_map"),
                            _model.state_key.like("%_request_count"),
                            _model.state_key.like("%:tpm:%"),
                            _model.state_key.like("%:rpm:%"),
                            _model.state_key.like("deployment:%:cooldown"),
                        ))
                    rows = query.limit(100).all()
                    for row in rows:
                        raw = row.state_value.get("v") if isinstance(row.state_value, dict) else None
                        if raw is None:
                            continue
                        try:
                            decoded = json.loads(raw) if isinstance(raw, str) else float(raw)
                        except Exception:
                            decoded = None
                        if decoded is None:
                            continue
                        if _is_cooldown_key(row.state_key):
                            l2_cooldown_entries[row.state_key] = decoded
                        elif _is_map_key(row.state_key):
                            l2_map_entries[row.state_key] = decoded
                        elif _is_counter_key(row.state_key):
                            l2_counter_entries[row.state_key] = decoded
                result["l2_keys"] = (
                    list(l2_map_entries.keys())
                    + list(l2_counter_entries.keys())
                    + list(l2_cooldown_entries.keys())
                )
                result["l2_size"] = len(result["l2_keys"])
                if show_values:
                    if l2_map_entries:
                        result["l2_map_values"] = l2_map_entries
                    if l2_counter_entries:
                        result["l2_counter_values"] = l2_counter_entries
                    if l2_cooldown_entries:
                        result["l2_cooldown_values"] = l2_cooldown_entries
            except Exception as e:
                result["l2_keys"] = []
                result["l2_error"] = str(e)
        else:
            result["l2_keys"] = []
            result["l2_note"] = "DB not initialized, using in-memory fallback only"
    else:
        l2_alt = getattr(dual, "redis_cache", None)
        if l2_alt:
            result["l2_cache_type"] = type(l2_alt).__name__

    if client_id:
        result["filter_client_id"] = client_id
    result["per_client_routing_enabled"] = os.environ.get(
        "ENABLE_PER_CLIENT_LATENCY_ROUTING", "false"
    ).lower() == "true"

    # --- Build deployment lookup maps from router model_list ---
    id_to_dep: dict = {}
    model_group_to_dep_ids: dict = {}  # model_group_name -> set of 64-char deployment IDs
    for m in getattr(router, 'model_list', []):
        lp = m.get('litellm_params', {})
        dep_info = {
            "model": lp.get('model', ''),
            "api_base": lp.get('api_base', ''),
            "timeout": lp.get('timeout'),
            "rpm_limit": lp.get('rpm'),
            "tpm_limit": lp.get('tpm'),
            "input_cost_per_token": lp.get('input_cost_per_token'),
            "output_cost_per_token": lp.get('output_cost_per_token'),
        }
        mid = m.get('model_info', {}).get('id', '')
        mg = m.get('model_name', '')
        if mid:
            id_to_dep[mid] = dep_info
            if mg:
                model_group_to_dep_ids.setdefault(mg, set()).add(mid)

    # --- routing_groups: decoded live state per group ---
    # Merge L2 under L1 so cold-L1 pods still show deployment data from Postgres
    effective_cache: dict = {
        **l2_map_entries, **l2_counter_entries, **l2_cooldown_entries, **cache_dict,
    }
    routing_groups_cfg = getattr(router, '_routing_groups', {})

    def _cooldown_status(dep_id: str) -> dict:
        """Reads the SAME cache CooldownCache writes to (deployment:{id}:cooldown).
        Computed against timestamp+cooldown_time ourselves rather than trusting
        key presence, since we read the raw dict directly and bypass the
        cache's own lazy-expiry check that get_cache() would normally apply."""
        raw = effective_cache.get(f"deployment:{dep_id}:cooldown")
        if not isinstance(raw, dict):
            return {"in_cooldown": False, "cooldown_expires_in_s": None, "cooldown_reason": None}
        ts = raw.get("timestamp")
        cd = raw.get("cooldown_time")
        if not isinstance(ts, (int, float)) or not isinstance(cd, (int, float)):
            return {"in_cooldown": False, "cooldown_expires_in_s": None, "cooldown_reason": None}
        remaining = (ts + cd) - time.time()
        if remaining <= 0:
            return {"in_cooldown": False, "cooldown_expires_in_s": None, "cooldown_reason": None}
        return {
            "in_cooldown": True,
            "cooldown_expires_in_s": round(remaining, 1),
            "cooldown_reason": raw.get("exception_received"),
        }

    def _base_deployment_entry(dep_id: str) -> dict:
        dep_info = id_to_dep.get(dep_id, {})
        entry = {
            "deployment_id": dep_id,
            "model": dep_info.get("model") or dep_id[:16] + "...",
            "api_base": dep_info.get("api_base", ""),
            "timeout_configured_s": dep_info.get("timeout"),
        }
        entry.update(_cooldown_status(dep_id))
        return entry

    def _usage_deployments(dep_rpm: dict, dep_tpm: dict) -> list:
        """Shared builder for usage-based-routing v1 and v2. Both are keyed by
        deployment_id by the time they reach here, only how they got parsed out
        of the cache differs, so the output shape is identical for both."""
        deployments = []
        for dep_id in sorted(set(list(dep_rpm.keys()) + list(dep_tpm.keys()))):
            info = id_to_dep.get(dep_id, {})
            curr_rpm = int(dep_rpm.get(dep_id, 0))
            curr_tpm = int(dep_tpm.get(dep_id, 0))
            rpm_limit = info.get("rpm_limit")
            tpm_limit = info.get("tpm_limit")
            dep_entry = _base_deployment_entry(dep_id)
            dep_entry["metrics"] = {
                "current_rpm": curr_rpm,
                "rpm_limit": rpm_limit,
                "rpm_utilization_pct": round(curr_rpm / rpm_limit * 100, 1) if rpm_limit else None,
                "current_tpm": curr_tpm,
                "tpm_limit": tpm_limit,
                "tpm_utilization_pct": round(curr_tpm / tpm_limit * 100, 1) if tpm_limit else None,
            }
            deployments.append(dep_entry)
        return deployments

    routing_groups_out: dict = {}
    for group_name, rg in routing_groups_cfg.items():
        strategy = rg.routing_strategy
        entry: dict = {
            "strategy": strategy,
            "models": rg.models or [],
            "routing_strategy_args": rg.routing_strategy_args or {},
            "notes": [],
        }

        if strategy in ("latency-based-routing", "cost-based-routing"):
            # LiteLLM stores the latency map keyed by MODEL GROUP NAME (the model name
            # passed to the router), not by routing group name. Search each model in
            # the group. cost-based-routing shares this same {model_group}_map key but
            # its per-minute entries never contain "latency" or "time_to_first_token"
            # (cost itself is computed live from litellm.model_cost, never cached), so
            # we compute an estimated cost live for that strategy instead.
            deps = []
            dep_id_seen: set = set()
            for model_name in (rg.models or []):
                map_key = f"{model_name}_map"
                scoped_key = f"cid:{client_id}:{map_key}" if client_id else None
                map_data = (effective_cache.get(scoped_key) if scoped_key else None) or effective_cache.get(map_key)
                # With per-client routing enabled, keys are cid-prefixed; scan all keys when no client_id
                if map_data is None and not client_id:
                    suffix = f":{map_key}"
                    for ck in effective_cache:
                        if ck.startswith("cid:") and ck.endswith(suffix):
                            map_data = effective_cache[ck]
                            break
                if not isinstance(map_data, dict):
                    continue
                for dep_id, dep_data in map_data.items():
                    if not isinstance(dep_data, dict) or dep_id in dep_id_seen:
                        continue
                    dep_id_seen.add(dep_id)
                    latency_arr = [float(x) for x in dep_data.get("latency", [])]
                    # time_to_first_token is a real, separately-cached list used by
                    # litellm to rank deployments for streaming requests. It's a list,
                    # not a dict, so the old traffic_history filter silently dropped it.
                    ttft_arr = [float(x) for x in dep_data.get("time_to_first_token", [])]
                    traffic_history = {
                        k: v for k, v in dep_data.items()
                        if k not in ("latency", "time_to_first_token") and isinstance(v, dict)
                    }
                    total_rpm = sum(v.get("rpm", 0) for v in traffic_history.values())
                    dep_entry = _base_deployment_entry(dep_id)
                    dep_entry["metrics"] = {
                        "avg_latency_s": round(sum(latency_arr) / len(latency_arr), 3) if latency_arr else None,
                        "latency_samples_s": [round(x, 6) for x in latency_arr],
                        "avg_ttft_s": round(sum(ttft_arr) / len(ttft_arr), 3) if ttft_arr else None,
                        "ttft_samples_s": [round(x, 6) for x in ttft_arr],
                        "traffic_history": traffic_history,
                        "total_rpm_in_history": total_rpm,
                    }
                    if strategy == "cost-based-routing":
                        dep_info = id_to_dep.get(dep_id, {})
                        input_cost = dep_info.get("input_cost_per_token")
                        output_cost = dep_info.get("output_cost_per_token")
                        if input_cost is None or output_cost is None:
                            model_cost_map = getattr(litellm, "model_cost", {}).get(
                                dep_info.get("model", ""), {}
                            )
                            if input_cost is None:
                                input_cost = model_cost_map.get("input_cost_per_token")
                            if output_cost is None:
                                output_cost = model_cost_map.get("output_cost_per_token")
                        dep_entry["metrics"]["estimated_input_cost_per_token"] = input_cost
                        dep_entry["metrics"]["estimated_output_cost_per_token"] = output_cost
                    deps.append(dep_entry)
            # Sorted for readability only - this is NOT what litellm actually
            # selects next. See module docstring re: currently_preferred.
            deps.sort(
                key=lambda x: float("inf")
                if x["metrics"].get("avg_latency_s") is None
                else x["metrics"]["avg_latency_s"]
            )
            entry["deployments"] = deps
            if deps:
                entry["notes"].append(
                    "deployments listed ascending by avg_latency_s for readability only; "
                    "this ordering is not litellm's actual selection (see function docstring)"
                )
            if any(d["metrics"].get("avg_ttft_s") is not None for d in deps):
                entry["notes"].append(
                    "litellm ranks by avg_ttft_s instead of avg_latency_s for streaming "
                    "requests when TTFT samples exist"
                )
            if strategy == "cost-based-routing":
                entry["notes"].append(
                    "estimated_*_cost_per_token is computed live from litellm_params / "
                    "litellm.model_cost, not read from cache - cost-based-routing never "
                    "persists cost data, so there is nothing to inspect in the cache for it"
                )

        elif strategy in ("usage-based-routing-v2", "usage-based-routing"):
            if strategy == "usage-based-routing-v2":
                # Build set of dep_ids belonging to THIS routing group's models
                # to avoid mixing deployments from other routing groups
                group_dep_ids: set = set()
                for mn in (rg.models or []):
                    group_dep_ids.update(model_group_to_dep_ids.get(mn, set()))
                dep_rpm: dict = {}
                dep_tpm: dict = {}
                for k, v in effective_cache.items():
                    metric = ":rpm:" if ":rpm:" in k else (":tpm:" if ":tpm:" in k else None)
                    if not metric:
                        continue
                    idx = k.rfind(metric)
                    before = k[:idx]
                    # global_router:{id}:{model}:tpm/rpm:{minute} is written by
                    # router.py's deployment_callback_on_success for cross-process
                    # telemetry, but litellm NEVER reads it back anywhere - the real
                    # selection logic (lowest_tpm_rpm_v2.async_get_available_deployments)
                    # only reads the unprefixed {id}:{model}:tpm/rpm:{minute} key. Both
                    # exist simultaneously for the same deployment when tpm/rpm limits
                    # are configured, so treating them as equivalent double-counts.
                    # Skip the telemetry-only prefixed one entirely.
                    if before.startswith("global_router:"):
                        continue
                    # Every routing-strategy handler used ANYWHERE in routing_groups is
                    # registered as a global litellm success callback, none of them are
                    # scoped to "their" group. So usage-based-routing (v1) writes
                    # {model_group}:tpm/rpm:{minute} -> {deployment_id: count} for every
                    # model in the router, including ones that belong to this v2 group,
                    # and other v2 groups' own {id}:{model}:tpm/rpm:{minute} keys are
                    # also visible here. A real v2 key for THIS deployment always has
                    # the strict {64-char-hex-deployment-id}:{name} shape; anything else
                    # is a different tracker's key that happens to contain ":tpm:"/":rpm:"
                    # - skip it instead of trying to parse it as v2 data.
                    if not (
                        len(before) >= 65
                        and before[64] == ":"
                        and all(c in "0123456789abcdef" for c in before[:64])
                    ):
                        continue
                    dep_id_hex = before[:64]
                    # Skip deployments not in this routing group
                    if group_dep_ids and dep_id_hex not in group_dep_ids:
                        continue
                    if not isinstance(v, (int, float)):
                        # defense in depth: ignore anything that isn't a plain number
                        continue
                    bucket = dep_rpm if metric == ":rpm:" else dep_tpm
                    bucket[dep_id_hex] = bucket.get(dep_id_hex, 0) + float(v)
                entry["deployments"] = _usage_deployments(dep_rpm, dep_tpm)
                if entry["deployments"]:
                    entry["notes"].append(
                        "counters expire after 60s per minute-window; L2 may be empty between minutes"
                    )

            else:  # usage-based-routing (v1): key is {model_group}:tpm/rpm:{minute}
                # NOTE: the cached value here is {deployment_id: count}, not a scalar.
                # Aggregate per deployment_id so the output shape matches v2 exactly.
                now_min = _dt.utcnow().strftime("%H-%M")
                dep_rpm: dict = {}
                dep_tpm: dict = {}
                for model_name in (rg.models or []):
                    rpm_raw = effective_cache.get(f"{model_name}:rpm:{now_min}", {}) or {}
                    tpm_raw = effective_cache.get(f"{model_name}:tpm:{now_min}", {}) or {}
                    if isinstance(rpm_raw, dict):
                        for dep_id, cnt in rpm_raw.items():
                            if isinstance(cnt, (int, float)):
                                dep_rpm[dep_id] = dep_rpm.get(dep_id, 0) + float(cnt)
                    if isinstance(tpm_raw, dict):
                        for dep_id, cnt in tpm_raw.items():
                            if isinstance(cnt, (int, float)):
                                dep_tpm[dep_id] = dep_tpm.get(dep_id, 0) + float(cnt)
                entry["current_minute"] = now_min
                entry["deployments"] = _usage_deployments(dep_rpm, dep_tpm)

        elif strategy == "least-busy":
            # Key is {model_group}_request_count, one per model in rg.models
            lb_deps = []
            dep_id_seen_lb: set = set()
            for model_name in (rg.models or []):
                rc_data = effective_cache.get(f"{model_name}_request_count")
                if not isinstance(rc_data, dict):
                    continue
                for dep_id, count in rc_data.items():
                    if dep_id in dep_id_seen_lb:
                        continue
                    dep_id_seen_lb.add(dep_id)
                    dep_entry = _base_deployment_entry(dep_id)
                    dep_entry["metrics"] = {"in_flight_requests": count}
                    lb_deps.append(dep_entry)
            # Sorted for readability only, see note above.
            lb_deps.sort(key=lambda x: x["metrics"]["in_flight_requests"])
            entry["deployments"] = lb_deps
            if lb_deps:
                entry["notes"].append(
                    "deployments listed ascending by in_flight_requests for readability "
                    "only; this ordering is not litellm's actual selection (see function docstring)"
                )

        else:
            # simple-shuffle / provider-budget-routing: stateless, no per-deployment cache
            entry["deployments"] = []
            entry["notes"].append(
                "simple-shuffle / provider-budget-routing is stateless; there is no "
                "per-deployment cache state to inspect for this strategy"
            )

        if not entry["notes"]:
            del entry["notes"]
        routing_groups_out[group_name] = entry

    result["routing_groups"] = routing_groups_out
    result["total_routing_groups"] = len(routing_groups_cfg)
    result["global_timeout_s"] = getattr(litellm, "request_timeout", None)

    return result
