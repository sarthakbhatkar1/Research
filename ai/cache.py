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
    """

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
            for k in display_keys:
                v = cache_dict.get(k, _MISSING)
                if v is _MISSING:
                    # evicted between the snapshot above and this lookup, skip it
                    continue
                if _is_map_key(k):
                    map_vals[k] = v
                elif _is_counter_key(k):
                    counter_vals[k] = v
            if map_vals:
                result["in_memory_map_values"] = map_vals
            if counter_vals:
                result["in_memory_counter_values"] = counter_vals
    else:
        result["in_memory_keys"] = []
        result["in_memory_size_total"] = 0

    # --- L2: PostgresCache (actual DB query) ---
    # Always decoded regardless of show_values so routing_groups can fall back to L2 on cold L1
    l2_map_entries: dict = {}
    l2_counter_entries: dict = {}
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
                        # _map: latency/cost; _request_count: least-busy; :tpm:/:rpm:: usage-based
                        query = query.filter(or_(
                            _model.state_key.like("%_map"),
                            _model.state_key.like("%_request_count"),
                            _model.state_key.like("%:tpm:%"),
                            _model.state_key.like("%:rpm:%"),
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
                        if _is_map_key(row.state_key):
                            l2_map_entries[row.state_key] = decoded
                        elif _is_counter_key(row.state_key):
                            l2_counter_entries[row.state_key] = decoded
                result["l2_keys"] = list(l2_map_entries.keys()) + list(l2_counter_entries.keys())
                result["l2_size"] = len(result["l2_keys"])
                if show_values:
                    if l2_map_entries:
                        result["l2_map_values"] = l2_map_entries
                    if l2_counter_entries:
                        result["l2_counter_values"] = l2_counter_entries
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
    model_name_to_dep: dict = {}
    model_group_to_dep_ids: dict = {}  # model_group_name -> set of 64-char deployment IDs
    for m in getattr(router, 'model_list', []):
        lp = m.get('litellm_params', {})
        dep_info = {
            "model": lp.get('model', ''),
            "api_base": lp.get('api_base', ''),
            "timeout": lp.get('timeout'),
            "rpm_limit": lp.get('rpm'),
            "tpm_limit": lp.get('tpm'),
        }
        mid = m.get('model_info', {}).get('id', '')
        mg = m.get('model_name', '')
        if mid:
            id_to_dep[mid] = dep_info
            if mg:
                model_group_to_dep_ids.setdefault(mg, set()).add(mid)
        if lp.get('model'):
            model_name_to_dep[lp['model']] = dep_info

    # --- routing_groups: decoded live state per group ---
    # Merge L2 under L1 so cold-L1 pods still show deployment data from Postgres
    effective_cache: dict = {**l2_map_entries, **l2_counter_entries, **cache_dict}
    routing_groups_cfg = getattr(router, '_routing_groups', {})
    from datetime import datetime as _dt

    routing_groups_out: dict = {}
    for group_name, rg in routing_groups_cfg.items():
        strategy = rg.routing_strategy
        entry: dict = {
            "strategy": strategy,
            "models": rg.models or [],
            "routing_strategy_args": rg.routing_strategy_args or {},
        }

        if strategy in ("latency-based-routing", "cost-based-routing"):
            # LiteLLM stores the latency map keyed by MODEL GROUP NAME (the model name
            # passed to the router), not by routing group name. Search each model in
            # the group. cost-based-routing shares this same {model_group}_map key but
            # its per-minute entries never contain "latency" or "time_to_first_token"
            # (cost itself is computed live from litellm.model_cost, never cached) -
            # so avg_latency_s / avg_ttft_s will legitimately be None for those groups.
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
                    dep_info = id_to_dep.get(dep_id, {})
                    deps.append({
                        "deployment_id": dep_id,
                        "model": dep_info.get("model") or dep_id[:16] + "...",
                        "api_base": dep_info.get("api_base", ""),
                        "timeout_configured_s": dep_info.get("timeout"),
                        "avg_latency_s": round(sum(latency_arr) / len(latency_arr), 3) if latency_arr else None,
                        "latency_samples_s": [round(x, 6) for x in latency_arr],
                        "avg_ttft_s": round(sum(ttft_arr) / len(ttft_arr), 3) if ttft_arr else None,
                        "ttft_samples_s": [round(x, 6) for x in ttft_arr],
                        "traffic_history": traffic_history,
                        "total_rpm_in_history": total_rpm,
                        "currently_preferred": False,
                    })
            deps.sort(key=lambda x: float("inf") if x.get("avg_latency_s") is None else x["avg_latency_s"])
            if deps:
                deps[0]["currently_preferred"] = True
                deps[0]["note"] = (
                    "Ranked by avg_latency_s; live routing uses avg_ttft_s instead "
                    "for streaming requests when TTFT samples exist"
                )
            entry["deployments"] = deps

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
                    if before.startswith("global_router:"):
                        before = before[14:]
                    if len(before) >= 65 and before[64] == ":" and all(c in "0123456789abcdef" for c in before[:64]):
                        dep_id_hex = before[:64]
                        dep_name = before[65:]
                    else:
                        dep_id_hex = ""
                        dep_name = before
                    # Skip deployments not in this routing group
                    if group_dep_ids and dep_id_hex and dep_id_hex not in group_dep_ids:
                        continue
                    bucket = dep_rpm if metric == ":rpm:" else dep_tpm
                    bucket[dep_name] = bucket.get(dep_name, 0) + (float(v) if v else 0)
                deployments_v2 = []
                for dep_name in sorted(set(list(dep_rpm.keys()) + list(dep_tpm.keys()))):
                    info = model_name_to_dep.get(dep_name, {})
                    curr_rpm = int(dep_rpm.get(dep_name, 0))
                    curr_tpm = int(dep_tpm.get(dep_name, 0))
                    rpm_limit = info.get("rpm_limit")
                    tpm_limit = info.get("tpm_limit")
                    deployments_v2.append({
                        "model": dep_name,
                        "api_base": info.get("api_base", ""),
                        "timeout_configured_s": info.get("timeout"),
                        "current_rpm": curr_rpm,
                        "rpm_limit": rpm_limit,
                        "rpm_utilization_pct": round(curr_rpm / rpm_limit * 100, 1) if rpm_limit else None,
                        "current_tpm": curr_tpm,
                        "tpm_limit": tpm_limit,
                        "tpm_utilization_pct": round(curr_tpm / tpm_limit * 100, 1) if tpm_limit else None,
                    })
                entry["deployments"] = deployments_v2
                if deployments_v2:
                    entry["note"] = "Counters expire after 60s per minute-window; L2 may be empty between minutes"

            else:  # usage-based-routing (v1): key is {model_group}:tpm/rpm:{minute}
                # NOTE: litellm stores this value as {deployment_id: count}, NOT a
                # scalar. int()'ing it directly raises TypeError as soon as the key
                # has any real data - that's the crash. Sum the per-deployment counts
                # instead, and keep the breakdown for extra visibility.
                now_min = _dt.utcnow().strftime("%H-%M")
                v1_models = []
                for model_name in (rg.models or []):
                    rpm_raw = effective_cache.get(f"{model_name}:rpm:{now_min}", {}) or {}
                    tpm_raw = effective_cache.get(f"{model_name}:tpm:{now_min}", {}) or {}
                    rpm_by_dep = rpm_raw if isinstance(rpm_raw, dict) else {}
                    tpm_by_dep = tpm_raw if isinstance(tpm_raw, dict) else {}
                    curr_rpm = sum(int(v) for v in rpm_by_dep.values() if isinstance(v, (int, float)))
                    curr_tpm = sum(int(v) for v in tpm_by_dep.values() if isinstance(v, (int, float)))
                    v1_models.append({
                        "model_group": model_name,
                        "current_rpm": curr_rpm,
                        "current_tpm": curr_tpm,
                        "current_rpm_by_deployment": rpm_by_dep,
                        "current_tpm_by_deployment": tpm_by_dep,
                    })
                entry["current_minute"] = now_min
                entry["model_groups"] = v1_models

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
                    dep_info = id_to_dep.get(dep_id, {})
                    lb_deps.append({
                        "model": dep_info.get("model") or dep_id[:16] + "...",
                        "api_base": dep_info.get("api_base", ""),
                        "timeout_configured_s": dep_info.get("timeout"),
                        "in_flight_requests": count,
                        "currently_preferred": False,
                    })
            lb_deps.sort(key=lambda x: x["in_flight_requests"])
            if lb_deps:
                lb_deps[0]["currently_preferred"] = True
            entry["deployments"] = lb_deps

        else:
            # simple-shuffle / provider-budget-routing: stateless, no per-deployment cache
            entry["deployments"] = []

        routing_groups_out[group_name] = entry

    result["routing_groups"] = routing_groups_out
    result["total_routing_groups"] = len(routing_groups_cfg)
    result["global_timeout_s"] = getattr(litellm, "request_timeout", None)

    return result
