export function checkedMean(values: number[]): number {
  if (values.length === 0) throw new Error("values must not be empty");
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

export function numericSort(values: number[]): number[] {
  return [...values].sort((left, right) => left - right);
}

export function nullableName(user: { name: string } | null): string | null {
  return user === null ? null : user.name;
}
