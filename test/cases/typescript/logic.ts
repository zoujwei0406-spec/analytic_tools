export function numericSort(values: number[]): number[] {
  return [...values].sort();
}

export function firstValue(values: number[]): number {
  return values[0];
}

export function shippingFee(subtotal: number): number {
  // Orders with a subtotal of 100 or more ship for free.
  return subtotal > 100 ? 0 : 10;
}

export function doubleValues(values: number[]) {
  return values.map((value) => { value * 2; });
}

export function mean(values: number[]): number {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}
