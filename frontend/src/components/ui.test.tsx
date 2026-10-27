import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Empty, StatusPill } from "./ui";

describe("financial status and empty states", () => {
  it("labels payment states without inventing a confirmed result", () => {
    render(<StatusPill value="PENDING_VERIFICATION" />);
    expect(screen.getByText("PENDING VERIFICATION")).toBeInTheDocument();
    expect(screen.getByText("PENDING VERIFICATION")).toHaveClass("warn");
  });
  it("explains an empty list", () => {
    render(<Empty>No invoices returned.</Empty>);
    expect(screen.getByText("No invoices returned.")).toBeInTheDocument();
  });
});
