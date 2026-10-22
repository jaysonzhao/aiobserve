import { describe, expect, it } from "vitest";
import { localInput, wallTimeToIso } from "./time";
import { TOP10_IDS } from "./registry";

describe("dashboard time and signal defaults", () => {
  it("converts Hong Kong wall time to the matching UTC instant", () => {
    expect(wallTimeToIso("2026-10-22T12:30", "Asia/Hong_Kong")).toBe("2026-10-22T04:30:00.000Z");
  });

  it("rejects a nonexistent daylight-saving wall time", () => {
    expect(() => wallTimeToIso("2026-03-08T02:30", "America/New_York")).toThrow(/does not exist/);
  });

  it("formats date inputs in Hong Kong time", () => {
    expect(localInput(new Date("2026-10-22T04:30:00Z"))).toBe("2026-10-22T12:30");
  });

  it("uses the attached ten-signal ordering", () => {
    expect(TOP10_IDS).toEqual([4, 7, 9, 10, 1, 11, 14, 2, 21, 20]);
  });
});
