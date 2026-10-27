"use client";
import { useRef } from "react";

/**
 * Provides a stable `Idempotency-Key` for a user-intended creation operation.
 *
 * The same key is reused for as long as the request signature (the exact payload
 * the backend will validate) is unchanged, so retrying an uncertain request —
 * after a timeout or ambiguous response — replays the original result instead of
 * creating a duplicate financial record. A genuinely new operation (a different
 * payload) receives a fresh key. `reset()` is called after a confirmed success so
 * the next intended creation starts with a new key.
 */
export function useIdempotencyKey() {
  const state = useRef<{ signature: string; key: string }>({ signature: "", key: "" });
  return {
    keyFor(signature: string): string {
      if (state.current.signature !== signature) {
        state.current = { signature, key: crypto.randomUUID() };
      }
      return state.current.key;
    },
    reset(): void {
      state.current = { signature: "", key: "" };
    },
  };
}
