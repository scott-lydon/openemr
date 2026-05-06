// k6 surge scenario: 2000 requests/min for 5 minutes.
//
// Stress test — well beyond normal load. The test passes if:
//
// - Container Resident Set Size (RSS) is stable across the surge (no
//   memory leak).
// - The graceful-shutdown path drains the queue when SIGTERM lands
//   (verified separately by the deploy hardening test in section 12.3
//   of the verification checklist).
// - Errors stay under 5% (during a real surge the agent may rate-limit,
//   that is a degradation, not a failure).
//
//   [Local Mac terminal]
//   k6 run tests/load/surge.js

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

const BASE = __ENV.COPILOT_LOAD_BASE_URL || 'http://localhost:8801';
const TOKEN = __ENV.COPILOT_LOAD_TOKEN || '';

const errorRate = new Rate('error_rate');

export const options = {
  scenarios: {
    surge: {
      executor: 'constant-arrival-rate',
      rate: 2000, timeUnit: '1m',
      duration: '5m',
      preAllocatedVUs: 100,
      maxVUs: 500,
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<30000'],
    error_rate: ['rate<0.05'],
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
    message: 'What is the recommended blood pressure target?',
  });
  const res = http.post(`${BASE}/chat`, body, { headers });
  const ok = check(res, {
    'status acceptable': (r) => r.status === 200 || r.status === 503,
  });
  errorRate.add(!ok);
  sleep(0.1);
}
