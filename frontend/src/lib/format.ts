export function money(value: string | number | null | undefined, currency = "KES", digits = 2): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const number = Number(value);
  if (!Number.isFinite(number)) return "Unavailable";
  return new Intl.NumberFormat("en-KE", { style: "currency", currency, currencyDisplay: "code", minimumFractionDigits: digits, maximumFractionDigits: Math.max(digits, 4) }).format(number);
}
export function metricText(value: { status?: string; formatted_value?: string; value?: string | number | null; unit?: string; currency?: string | null }, currency: string): string {
  if (!value || value.value === null || value.value === undefined || value.status && value.status !== "available") return "Not available";
  if (value.formatted_value) return value.formatted_value;
  if (value.unit === "currency") return money(value.value, value.currency || currency);
  return `${value.value}${value.unit === "percentage" ? "%" : value.unit === "ratio" || value.unit === "multiplier" ? "×" : value.unit === "days" ? " days" : ""}`;
}
export function dateOnly(date = new Date()) { return date.toISOString().slice(0, 10); }
