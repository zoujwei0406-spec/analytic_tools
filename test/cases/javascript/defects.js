export function evaluateText(text) {
  return eval(text);
}

export function buildFunction(expression) {
  return new Function("value", "return " + expression);
}

export function calculateTotal(price) {
  return price + missingFee;
}

export function changeConstant() {
  const total = 1;
  total = 2;
  return total;
}

export function sameValue(value) {
  return value === value;
}

export function unreachableCharge() {
  return 10;
  console.log("charged");
}

export function duplicatedConfig() {
  return { retries: 1, retries: 10 };
}

export function checkInFinally() {
  try {
    throw new Error("payment failed");
  } finally {
    return true;
  }
}

export function loadValue(loader) {
  return new Promise(async (resolve) => {
    const value = await loader();
    resolve(value);
  });
}

export function readNull() {
  const user = null;
  return user.name;
}
