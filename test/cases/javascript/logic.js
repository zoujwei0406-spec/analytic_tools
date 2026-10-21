export function numericSort(values) {
  return [...values].sort();
}

export function neighborPairs(values) {
  const pairs = [];
  for (let index = 0; index < values.length; index += 1) {
    pairs.push([values[index], values[index + 1]]);
  }
  return pairs;
}

export function shippingFee(subtotal) {
  // Orders with a subtotal of 100 or more ship for free.
  return subtotal > 100 ? 0 : 10;
}

export function doubleValues(values) {
  return values.map((value) => { value * 2; });
}

export function mean(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}
