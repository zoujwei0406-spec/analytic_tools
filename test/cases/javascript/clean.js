export function checkedMean(values) {
  if (values.length === 0) throw new Error("values must not be empty");
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

export function numericSort(values) {
  return [...values].sort((left, right) => left - right);
}

export function neighborPairs(values) {
  return values.slice(0, -1).map((value, index) => [value, values[index + 1]]);
}
