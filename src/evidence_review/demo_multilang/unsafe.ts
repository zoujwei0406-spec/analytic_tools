// Deliberate unsafe eval; parsed as TypeScript, not plain JavaScript.
export function parseInput(text: string): unknown {
  return eval(text);
}

export const handleInput = (text: string): unknown => parseInput(text);
