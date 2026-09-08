import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "./CommandPalette";
import { clearRegistry, registerPanel } from "../commands/registry";
import { Layout } from "../commands/types";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function mockFetch(handler: (url: string) => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => handler(String(input)));
}

// jsdom has no ResizeObserver; cmdk uses one to track its list height.
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", FakeResizeObserver);
// jsdom does not implement scrollIntoView either; cmdk calls it when the
// active item changes.
Element.prototype.scrollIntoView = vi.fn();

function Harness(props: { onPick: (text: string) => void; onClose: () => void }) {
  const [query, setQuery] = useState("");
  return <CommandPalette open query={query} onQuery={setQuery} onPick={props.onPick} onClose={props.onClose} />;
}

beforeEach(() => {
  clearRegistry();
  registerPanel({ code: "DES", title: "Description", needsSymbol: true, layout: Layout.Headed, parseArgs: () => ({}), component: () => null });
  registerPanel({ code: "GIP", title: "Intraday", needsSymbol: true, layout: Layout.Headed, parseArgs: () => ({}), component: () => null });
  registerPanel({ code: "HELP", title: "Help", needsSymbol: false, layout: Layout.Single, parseArgs: () => ({}), component: () => null });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("CommandPalette", () => {
  it("filters mnemonics as the query changes", async () => {
    mockFetch((url) => {
      throw new Error(`unexpected ${url}`);
    });
    render(<Harness onPick={() => {}} onClose={() => {}} />);
    expect(screen.getByText(/DES —/)).toBeInTheDocument();
    expect(screen.getByText(/GIP —/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("palette"), "GI");
    expect(screen.queryByText(/DES —/)).not.toBeInTheDocument();
    expect(screen.getByText(/GIP —/)).toBeInTheDocument();
  });

  it("says what search matches and how to open a pick beside the panel", () => {
    mockFetch((url) => {
      throw new Error(`unexpected ${url}`);
    });
    render(<Harness onPick={() => {}} onClose={() => {}} />);
    expect(screen.getByText(/Search matches the ticker or the company name/)).toBeInTheDocument();
    expect(screen.getByText("Ctrl+Enter")).toBeInTheDocument();
  });

  it("searches by ticker or name once the query is long enough, and picking one calls onPick", async () => {
    mockFetch((url) => {
      // The terminal's own read, not `/v1/symbols?q=`: that one matches
      // the symbol column only and would never find "Microsoft".
      if (url.startsWith("/ui/api/search?q=")) {
        return json(200, {
          data: [{ symbol: "MSFT", long_name: "Microsoft", short_name: null, exchange: "NMS", quote_type: null }],
          next_cursor: null,
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    const onPick = vi.fn();
    render(<Harness onPick={onPick} onClose={() => {}} />);
    await userEvent.type(screen.getByLabelText("palette"), "microsoft");
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/ui/api/search?q=microsoft"),
      expect.anything(),
    ));
    const item = await screen.findByText(/MSFT — Microsoft/);
    await userEvent.click(item);
    expect(onPick).toHaveBeenCalledWith("MSFT", false);
  });

  it("opens a pick beside the panel when the modifier is held", async () => {
    mockFetch((url) => {
      if (url.startsWith("/ui/api/search?q=")) {
        return json(200, {
          data: [{ symbol: "MSFT", long_name: "Microsoft", short_name: null, exchange: "NMS", quote_type: null }],
          next_cursor: null,
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    const onPick = vi.fn();
    render(<Harness onPick={onPick} onClose={() => {}} />);
    await userEvent.type(screen.getByLabelText("palette"), "microsoft");
    const item = await screen.findByText(/MSFT — Microsoft/);
    // `fireEvent`, because the modifier IS the assertion: userEvent's
    // held-key syntax does not put `ctrlKey` on the pointer event, and a
    // test that silently sent a plain click would have passed against a
    // component that ignored the modifier entirely.
    fireEvent.click(item, { ctrlKey: true });
    expect(onPick).toHaveBeenCalledWith("MSFT", true);
  });

  it("closes on Escape", async () => {
    mockFetch((url) => {
      throw new Error(`unexpected ${url}`);
    });
    const onClose = vi.fn();
    render(<Harness onPick={() => {}} onClose={onClose} />);
    await userEvent.type(screen.getByLabelText("palette"), "{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});
