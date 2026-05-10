import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { middleware } from "@/middleware";

describe("middleware: CSP override", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("appends COPILOT_URL origin to frame-src when set (allows iframe embed)", () => {
    vi.stubEnv("COPILOT_URL", "https://copilot.example.com");
    const res = middleware();
    const csp = res.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("frame-src 'self' https://copilot.example.com");
  });

  it("frame-src is just 'self' when COPILOT_URL is unset", () => {
    vi.stubEnv("COPILOT_URL", "");
    const res = middleware();
    const csp = res.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("frame-src 'self'");
    expect(csp).not.toContain("https://copilot.example.com");
  });

  it("ignores invalid COPILOT_URL gracefully (no broken CSP)", () => {
    vi.stubEnv("COPILOT_URL", "not-a-url");
    const res = middleware();
    const csp = res.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("frame-src 'self'");
    // Confirm the rest of the CSP still rendered, not corrupted by the bad input
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("object-src 'none'");
  });

  it("only sets CSP — does not touch other security headers (kept static via next.config.ts)", () => {
    vi.stubEnv("COPILOT_URL", "https://copilot.example.com");
    const res = middleware();
    expect(res.headers.get("Content-Security-Policy")).not.toBeNull();
    // These come from next.config.ts headers() and should be untouched
    expect(res.headers.get("X-Content-Type-Options")).toBeNull();
    expect(res.headers.get("X-Frame-Options")).toBeNull();
  });
});
