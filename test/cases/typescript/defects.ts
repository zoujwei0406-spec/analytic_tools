export function evaluateText(text: string): unknown {
  return eval(text);
}

export function buildFunction(expression: string): Function {
  return new Function("value", "return " + expression);
}

export function wrongAssignment(): number {
  const count: number = "three";
  return count;
}

export function wrongReturn(): number {
  return "42";
}

export function missingProperty(user: { name: string }): string {
  return user.email;
}

export function nullableName(user: { name: string } | null): string {
  return user.name;
}

export function callAsFunction(): number {
  const value: number = 42;
  return value();
}

export function numberLabel(value: number): string {
  return value.toUpperCase();
}

export function changeConstant(): number {
  const count = 1;
  count = 2;
  return count;
}

export function swallowedFailure(): boolean {
  try {
    throw new Error("payment failed");
  } finally {
    return true;
  }
}
