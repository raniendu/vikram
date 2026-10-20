const words: Record<string, string> = {
  ai: "AI",
  awq: "AWQ",
  chatgpt: "ChatGPT",
  deepseek: "DeepSeek",
  ggml: "GGML",
  gguf: "GGUF",
  gpt: "GPT",
  gptq: "GPTQ",
  it: "IT",
  mlx: "MLX",
  oss: "OSS",
  qat: "QAT",
};

function formatWord(word: string): string {
  if (Object.hasOwn(words, word.toLowerCase())) return words[word.toLowerCase()];
  if (/^(?:[ae]?\d+(?:\.\d+)?[bm]|(?:i?q|fp?|bf|r)\d[\w.]*)$/i.test(word))
    return word.toUpperCase();
  return word.charAt(0).toUpperCase() + word.slice(1);
}

// Presentation only: selections, saved chats, and requests keep the Ollama ID.
export function formatModelName(id: string): string {
  const slash = id.lastIndexOf("/");
  const namespace = id.slice(0, slash + 1);
  const local = id.slice(slash + 1);
  const colon = local.indexOf(":");
  const family = colon < 0 ? local : local.slice(0, colon);
  const tag = colon < 0 ? "" : local.slice(colon + 1);
  const name = family
    .replace(/([a-z]{2,})(\d+(?:\.\d+)*)(?=$|[-_])/gi, "$1 $2")
    .split(/[-_\s]+/)
    .map(formatWord)
    .join(" ");
  const variant = tag.split("-").map(formatWord).join(" ");
  return `${namespace}${name}${variant ? ` · ${variant}` : ""}`;
}
