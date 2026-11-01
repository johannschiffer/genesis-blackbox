/**
 * Showcase 2: Order Routing & Verification (TypeScript)
 * Simulates business logic across TypeScript boundaries.
 */

export interface OrderCustomer {
  id: string;
  email: string;
  profile?: {
    tier: string;
    multiplier: number;
  };
}

export interface OrderItem {
  sku: string;
  quantity: number;
  price: number;
}

export interface OrderRequest {
  orderId: string;
  customer: OrderCustomer;
  items: OrderItem[];
}

export async function processOrderRoute(order: OrderRequest) {
  // Upstream schema regression: customer profile omitted
  // Accessing tier on undefined profile throws V8 TypeError
  const tier = order.customer.profile.tier;
  const multiplier = order.customer.profile.multiplier;
  const total = order.items.reduce((acc, it) => acc + it.price * it.quantity, 0) * multiplier;
  return {
    orderId: order.orderId,
    tier,
    total,
  };
}

