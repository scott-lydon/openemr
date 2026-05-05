// k6 baseline load scenario for the Week 2 sidecar.
//
// Runs at ~50 requests/min for 5 minutes — the steady-state target
// pattern for a single-clinic deployment. The agent's p95 latency on
// document upload should stay under 10 seconds and on chat under 5
// seconds. Errors should be zero.
//
// Run from clinical-copilot/ with:
//   [Local Mac terminal]
//   k6 run tests/load/baseline.js
//
// Configure target with COPILOT_LOAD_BASE_URL (defaults to localhost).
// Provide a dev token via COPILOT_LOAD_TOKEN.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const BASE = __ENV.COPILOT_LOAD_BASE_URL || 'http://localhost:8801';
const TOKEN = __ENV.COPILOT_LOAD_TOKEN || '';

const chatLatency = new Trend('chat_latency_ms');
const errorRate = new Rate('error_rate');

export const options = {
  scenarios: {
    baseline: {
      executor: 'constant-arrival-rate',
      rate: 50, timeUnit: '1m',
      duration: '5m',
      preAllocatedVUs: 10,
      maxVUs: 30,
    },
  },
  thresholds: {
    chat_latency_ms: ['p(95)<5000'],
    error_rate: ['rate<0.01'],
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const headers = {
    'Authorization': `Bearer ${TOKEN}`,
    'Content-Type': 'application/json',
  };
  const body = JSON.stringify({
    user_id: 'load-test',
    patient_id: 'Patient/87413',
    purpose: 'diagnostic_cross_check',
    message: 'What is the recommended HbA1c target for adults with type 2 diabetes?',
  });

  const t0 = Date.now();
  const res = http.post(`${BASE}/chat`, body, { headers });
  chatLatency.add(Date.now() - t0);
  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'response not empty': (r) => r.body.length > 0,
  });
  errorRate.add(!ok);
  sleep(1);
}
