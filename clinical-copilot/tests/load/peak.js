// k6 peak scenario: ~500 requests/min for 5 minutes.
//
// Simulates a busy multi-clinic peak. p95 chat latency may climb but
// must stay under 15 seconds and error rate must remain under 1%.
// Above these thresholds the test fails and the deployment is
// considered unprepared for peak load.
//
//   [Local Mac terminal]
//   COPILOT_LOAD_BASE_URL=https://staging.example.com k6 run tests/load/peak.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const BASE = __ENV.COPILOT_LOAD_BASE_URL || 'http://localhost:8801';
const TOKEN = __ENV.COPILOT_LOAD_TOKEN || '';

const chatLatency = new Trend('chat_latency_ms');
const errorRate = new Rate('error_rate');

export const options = {
  scenarios: {
    peak: {
      executor: 'constant-arrival-rate',
      rate: 500, timeUnit: '1m',
      duration: '5m',
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: {
    chat_latency_ms: ['p(95)<15000'],
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
    message: 'Should I screen this 55 year old male for colorectal cancer?',
  });
  const t0 = Date.now();
  const res = http.post(`${BASE}/chat`, body, { headers });
  chatLatency.add(Date.now() - t0);
  const ok = check(res, { 'status is 200': (r) => r.status === 200 });
  errorRate.add(!ok);
  sleep(0.5);
}
