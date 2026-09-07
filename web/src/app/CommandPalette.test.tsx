import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

  it("shows the prefix note", () => {
    mockFetch((url) => {
      throw new Error(`unexpected ${url}`);
    });
    render(<Harness onPick={() => {}} onClose={() => {}} />);
    expect(screen.getByText("Symbol search matches the start of the ticker only, not company names.")).toBeInTheDocument();
  });

  it("searches symbols once the query reaches the minimum prefix, and picking one calls onPick", async () => {
    mockFetch((url) => {
      if (url.startsWith("/ui/api/v1/symbols?q=MS")) {
        return json(200, {
          data: [{ symbol: "MSFT", long_name: "Microsoft", short_name: null, exchange: null, quote_type: null }],
          next_cursor: null,
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    const onPick = vi.fn();
    render(<Harness onPick={onPick} onClose={() => {}} />);
    await userEvent.type(screen.getByLabelText("palette"), "MS");
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/ui/api/v1/symbols?q=MS&limit=20"),
      expect.anything(),
    ));
    const item = await screen.findByText("MSFT — Microsoft");
    await userEvent.click(item);
    expect(onPick).toHaveBeenCalledWith("MSFT");
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
