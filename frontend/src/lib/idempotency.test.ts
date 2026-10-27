import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useIdempotencyKey } from "@/lib/idempotency";

let counter = 0;
function stubUuid() {
  Object.defineProperty(globalThis.crypto, "randomUUID", {
    configurable: true,
    writable: true,
    value: () => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`,
  });
}

describe("useIdempotencyKey", () => {
  beforeEach(() => { counter = 0; stubUuid(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it("reuses one key for an unchanged creation and issues a fresh key for a new operation", () => {
    const { result } = renderHook(() => useIdempotencyKey());

    const first = result.current.keyFor("payloadA");
    expect(first).toBe("00000000-0000-4000-8000-000000000001");
    // Retrying the same intended creation replays with the same key.
    expect(result.current.keyFor("payloadA")).toBe(first);
    // A genuinely different payload is a new operation and gets a new key.
    expect(result.current.keyFor("payloadB")).toBe("00000000-0000-4000-8000-000000000002");
  });

  it("starts a new key after reset so the next creation is independent", () => {
    const { result } = renderHook(() => useIdempotencyKey());
    const first = result.current.keyFor("payloadA");
    result.current.reset();
    const afterReset = result.current.keyFor("payloadA");
    expect(afterReset).not.toBe(first);
    expect(afterReset).toBe("00000000-0000-4000-8000-000000000002");
  });
});
