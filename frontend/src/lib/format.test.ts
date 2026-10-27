import { describe, expect, it } from "vitest";
import { metricText, money } from "./format";

describe("financial display formatting", () => {
  it("formats decimal money with the supplied currency", () => {
    expect(money("1234.5", "KES")).toContain("1,234.50");
    expect(money("1234.5", "KES")).toContain("KES");
  });
  it("does not turn missing or non-finite numbers into zero", () => {
    expect(money(null, "KES")).toBe("Unavailable");
    expect(money("NaN", "KES")).toBe("Unavailable");
    expect(metricText({ status: "insufficient_data", value: null, unit: "currency" }, "KES")).toBe("Not available");
  });
  it("uses metric-provided formatting and respects unavailable status", () => {
    expect(metricText({ status: "available", value: "12.3", formatted_value: "12.30%", unit: "percentage" }, "KES")).toBe("12.30%");
    expect(metricText({ status: "undefined", value: "0", unit: "ratio" }, "KES")).toBe("Not available");
  });
});
