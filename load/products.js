import http from 'k6/http';
import { Trend } from 'k6/metrics';

const rate = Number(__ENV.RATE || 1);
const duration = Number(__ENV.DURATION_S || 105);
if (!Number.isInteger(rate) || rate < 1 || rate > 5 ||
    !Number.isInteger(duration) || duration < 1 || duration > 1500) {
  throw new Error('RATE debe ser 1..5 y DURATION_S 1..1500 (enteros)');
}

const latency = new Trend('product_latency_ms', true);
export const options = {
  scenarios: {
    products: {
      executor: 'constant-arrival-rate', rate, timeUnit: '1s',
      duration: `${duration}s`, preAllocatedVUs: 32, maxVUs: 32,
      gracefulStop: '5s',
    },
  },
  // Los errores HTTP durante el corte son datos esperados. Perder iteraciones no.
  thresholds: { dropped_iterations: ['count==0'] },
};

export default function () {
  const started = Date.now();
  const response = http.get('http://127.0.0.1:8000/products?limit=10', {
    timeout: '4s', redirects: 0,
  });
  const ended = Date.now();
  let success = false;
  if (response.status === 200) {
    try {
      const body = response.json();
      success = Array.isArray(body) && body.length === 10 &&
        body.every((p) => Number.isInteger(p.id) && typeof p.name === 'string');
    } catch (_) { success = false; }
  }
  // Una muestra por consulta, incluidas fallas. Incluye conexión HTTP y recepción.
  latency.add(ended - started, {
    started_ms: String(started), ended_ms: String(ended),
    success: String(success), status: String(response.status),
  });
}
