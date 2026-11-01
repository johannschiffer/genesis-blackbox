import http from 'node:http';
import { processOrderRoute } from './routes.ts';

/**
 * Spins up an internal mock endpoint for verifying HTTP lifecycle capture.
 */
function createMockGateway() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', authorized: true }));
    });
    server.listen(0, '127.0.0.1', () => {
      resolve(server);
    });
  });
}

export async function handleCheckout() {
  const server = await createMockGateway();
  const port = server.address().port;

  try {
    // 1. Outbound HTTP fetch with sensitive credentials to test automatic redaction
    const url = `http://127.0.0.1:${port}/api/v1/auth/verify?api_key=live_secret_key_999&session_token=live_sess_123&client=demo`;
    await fetch(url, {
      method: 'GET',
      headers: {
        'Authorization': 'Bearer secret_jwt_access_token_123',
        'Cookie': 'session=sess_abcdef; api_key=key_xyz789',
        'X-Service-Name': 'checkout-coordinator',
        'X-Custom-Trace': 'trace-node-8899'
      }
    });
  } finally {
    server.close();
  }

  // 2. Upstream async order processing triggering unhandled V8 rejection
  await processOrderRoute({
    orderId: 'ORD-2026-V8',
    customer: {
      id: 'cust_771',
      email: 'alex@enterprise.corp'
      // Missing profile property triggers TypeError: Cannot read properties of undefined (reading 'tier')
    },
    items: [
      { sku: 'GPU-H100-POD', quantity: 2, price: 4500.0 }
    ]
  });
}

handleCheckout();

