// Static analysis fixture. It is not executed by the demo.
export const parseInput = (text) => eval(text);

export function handleInput(text) {
  return parseInput(text);
}
