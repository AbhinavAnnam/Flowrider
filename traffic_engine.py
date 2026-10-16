import numpy as np
import networkx as nx
from sklearn.ensemble import RandomForestRegressor

def generate_historical_telemetry(samples=100):
    """Simulates historical link utilization percentage (0.0 to 1.0)."""
    np.random.seed(42)
    time_steps = np.arange(samples)
    
    # Simulate utilization wave (0% to 100%)
    base_util = 0.4 + 0.3 * np.sin(time_steps / 5.0)
    bursts = np.random.choice([0.0, 0.35], size=samples, p=[0.8, 0.2])
    noise = np.random.normal(0, 0.05, size=samples)
    
    utilization_series = np.clip(base_util + bursts + noise, 0.05, 0.98)
    
    X = np.column_stack((time_steps[:-1], utilization_series[:-1]))
    y = utilization_series[1:]
    
    return X, y

def train_traffic_predictor():
    """Trains regressor to predict next-step link utilization ratio."""
    X, y = generate_historical_telemetry()
    model = RandomForestRegressor(n_estimators=50, random_state=42)
    model.fit(X, y)
    return model

def update_dynamic_weights(G, model, current_step):
    """Predicts utilization ratio and applies exponential weight penalty."""
    for u, v, data in G.edges(data=True):
        current_load = data['current_load']
        capacity = data['capacity']
        base_delay = data['base_delay']
        
        # Calculate live utilization ratio
        current_util = min(current_load / capacity, 1.0)
        
        # ML Prediction for near-future utilization ratio
        features = np.array([[current_step, current_util]])
        predicted_util = float(model.predict(features)[0])
        predicted_util = np.clip(predicted_util, 0.0, 1.0)
        
        # Dynamic Weight: Exponential penalty scaled heavily above 80% saturation
        dynamic_weight = base_delay * (1 + (predicted_util ** 4) * 25)
        
        G[u][v]['predicted_load'] = round(predicted_util * capacity, 2)
        G[u][v]['latency'] = round(dynamic_weight, 2)

def train_live_predictor(records, model_path, source='live_mininet', threshold=0.8,
                         min_train=60, min_test=20, test_fraction=0.2, holdout_session=None,
                         forest_parameters=None):
    """Train once offline, with chronological holdout and an elapsed-time purge."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import pickle
    import tempfile
    import sklearn
    from prediction import FEATURES, HORIZON, LOOKBACK, TOLERANCE, build_examples, chronological_split, evaluate

    if not records or {r['source'] for r in records} != {source}:
        raise ValueError('No observations or mixed/unexpected provenance; never mix live and synthetic data')
    if not 0 < threshold <= 1 or min_train < 2 or min_test < 2:
        raise ValueError('Invalid threshold or sample minimum')
    examples = build_examples(records)
    train, test = chronological_split(examples, test_fraction, holdout_session)
    if len(train) < min_train or len(test) < min_test:
        raise ValueError(f'Insufficient labeled data after purge: train={len(train)}, test={len(test)}; '
                         f'require {min_train}/{min_test}. Collect more independent traffic sessions.')
    parameters = dict(n_estimators=100, min_samples_leaf=2, random_state=42, n_jobs=1)
    if forest_parameters:
        if set(forest_parameters) - {'n_estimators', 'min_samples_leaf', 'max_depth', 'max_features'}:
            raise ValueError('Unsupported forest parameters; seed and worker count remain fixed')
        parameters.update(forest_parameters)
    model = RandomForestRegressor(**parameters)
    model.fit([r['features'] for r in train], [r['target'] for r in train])
    actual = [r['target'] for r in test]
    predicted = model.predict([r['features'] for r in test]).tolist()
    baseline = [r['utilization'] for r in test]
    digest = hashlib.sha256(json.dumps([(r['session_id'], r['link_id'], r['time'], r['features'], r['target'])
                                       for r in examples], sort_keys=True).encode()).hexdigest()
    version_digest = hashlib.sha256(json.dumps(dict(dataset=digest, source=source, threshold=threshold,
        features=FEATURES, horizon=HORIZON, parameters=model.get_params(), sklearn=sklearn.__version__),
        sort_keys=True).encode()).hexdigest()
    from telemetry_store import utc_now
    metadata = dict(schema_version=1, model_version='rf-' + version_digest[:12], dataset_sha256=digest,
        trained_timestamp=utc_now(),
        source=source, features=list(FEATURES), horizon_seconds=HORIZON, lookback_seconds=LOOKBACK,
        tolerance_seconds=TOLERANCE, threshold=threshold, sklearn_version=sklearn.__version__,
        numpy_version=np.__version__, hyperparameters=model.get_params(),
        train_count=len(train), test_count=len(test),
        train_latest_label_time=max(r['label_time'] for r in train),
        test_earliest_feature_time=min(r['time'] for r in test),
        test_fraction=test_fraction, holdout_session=holdout_session,
        train_sessions=sorted({r['session_id'] for r in train}),
        test_sessions=sorted({r['session_id'] for r in test}),
        train_positive_count=sum(r['target'] >= threshold for r in train),
        evaluation=dict(random_forest=evaluate(actual, predicted, threshold),
                        persistence=evaluate(actual, baseline, threshold)))
    # No scaler/imputer is fitted: incomplete feature windows are rejected.
    path = Path(model_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            pickle.dump(dict(model=model, metadata=metadata), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return metadata


if __name__ == "__main__":
    from network_topology import build_network_graph, calculate_shortest_path
    
    graph = build_network_graph()
    ml_model = train_traffic_predictor()
    
    # 1. Baseline route BEFORE congestion
    route_before = calculate_shortest_path(graph, "Router_1", "Router_4")
    print("--- BEFORE ML Dynamic Weighting ---")
    print(f"Path: {' -> '.join(route_before['path'])} | Latency: {route_before['total_latency_ms']} ms")
    
    # 2. Simulate 95% congestion spike on the backbone link
    graph["Router_2"]["Router_5"]["current_load"] = 1900 # 1900 / 2000 Mbps = 95%
    
    # 3. Trigger Control Plane ML weight recalculation
    update_dynamic_weights(graph, ml_model, current_step=10)
    
    # 4. Route AFTER ML penalty
    route_after = calculate_shortest_path(graph, "Router_1", "Router_4")
    print("\n--- AFTER ML Dynamic Weighting (Proactive Reroute) ---")
    print(f"Path: {' -> '.join(route_after['path'])} | Latency: {route_after['total_latency_ms']} ms")
