const rupees = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

/** Paise to a rupee string in Indian grouping, e.g. 200413400 -> "₹20,04,134". */
export function formatPaise(paise: number): string {
  return rupees.format(paise / 100);
}

export function formatPercent(part: number, whole: number): string {
  if (whole === 0) return "0%";
  return `${((part / whole) * 100).toFixed(1)}%`;
}

/** Rewrites "50000 paise" in backend text as "₹500". */
export function humanizePaise(text: string): string {
  return text.replace(/(\d+) paise/g, (_, n: string) => formatPaise(Number(n)));
}
