import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "./CommandPalette";
import { registerPanel } from "../commands/registry";
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

registerPanel({ code: "DES", title: "Description", needsSymbol: true, layout: Layout.Headed, parseArgs: () => ({}), component: () => null });
registerPanel({ code: "GIP", title: "Intraday", needsSymbol: true, layout: Layout.Headed, parseArgs: () => ({}), component: () => null });
registerPanel({ code: "HELP", title: "Help", needsSymbol: false, layout: Layout.Single, parseArgs: () => ({}), component: () => null });

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
    expect(screen.getByRole("option", { name: /DES —/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /GIP —/ })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("palette"), "GI");
    expect(screen.queryByRole("option", { name: /DES —/ })).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /GIP —/ })).toBeInTheDocument();
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
    const item = await screen.findByRole("option", { name: /MSFT — Microsoft/ });
    await userEvent.click(item);
    expect(onPick).toHaveBeenCalledWith("MSFT", false, "symbol");
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
    const item = await screen.findByRole("option", { name: /MSFT — Microsoft/ });
    // `fireEvent`, because the modifier IS the assertion: userEvent's
    // held-key syntax does not put `ctrlKey` on the pointer event, and a
    // test that silently sent a plain click would have passed against a
    // component that ignored the modifier entirely.
    fireEvent.click(item, { ctrlKey: true });
    expect(onPick).toHaveBeenCalledWith("MSFT", true, "symbol");
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

it("distinguishes a failed symbol search from an empty result", async () => {
  mockFetch(() => json(503, { title: "Search unavailable" }));
  render(<Harness onPick={() => {}} onClose={() => {}} />);
  await userEvent.type(screen.getByLabelText("palette"), "zz");
  expect(await screen.findByRole("alert")).toHaveTextContent(/search.*unavailable/i);
  expect(screen.getByRole("button", { name: "Retry search" })).toBeInTheDocument();
});

it("shows a useful empty result message", async () => {
  mockFetch(() => json(200, { data: [], next_cursor: null }));
  render(<Harness onPick={() => {}} onClose={() => {}} />);
  await userEvent.type(screen.getByLabelText("palette"), "zz");
  expect(await screen.findByText(/No symbols found/)).toBeInTheDocument();
});

it("removes old results immediately when the query changes", async () => {
  mockFetch(() => json(200, { data: [{ symbol: "AAPL", long_name: "Apple Inc.", short_name: null, exchange: "NMS", quote_type: "EQUITY" }], next_cursor: null }));
  render(<Harness onPick={() => {}} onClose={() => {}} />);
  const input = screen.getByLabelText("palette");
  await userEvent.type(input, "AA");
  await screen.findByRole("option", { name: /AAPL —/ });
  fireEvent.change(input, { target: { value: "ZZ" } });
  expect(screen.queryByRole("option", { name: /AAPL —/ })).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Searching symbols");
});

it("retries a failed search and shows the recovered results", async () => {
  let available = false;
  mockFetch(() => available ? json(200, { data: [{ symbol: "AAPL", long_name: "Apple Inc.", short_name: null, exchange: "NMS", quote_type: "EQUITY" }], next_cursor: null }) : json(503, { title: "Unavailable" }));
  render(<Harness onPick={() => {}} onClose={() => {}} />);
  await userEvent.type(screen.getByLabelText("palette"), "AA");
  await screen.findByRole("alert");
  available = true;
  await userEvent.click(screen.getByRole("button", { name: "Retry search" }));
  expect(await screen.findByRole("option", { name: /AAPL —/ })).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("distinguishes a symbol result from a function with the same code", async () => {
  mockFetch(() => json(200, { data: [{ symbol: "DES", long_name: "Example Security", exchange: "NMS" }], next_cursor: null }));
  const onPick = vi.fn();
  render(<Harness onPick={onPick} onClose={() => {}} />);
  await userEvent.type(screen.getByLabelText("palette"), "DES");
  await userEvent.click(await screen.findByRole("option", { name: /DES — Example Security/ }));
  expect(onPick).toHaveBeenLastCalledWith("DES", false, "symbol");
  await userEvent.click(screen.getByRole("option", { name: "DES — Description" }));
  expect(onPick).toHaveBeenLastCalledWith("DES", false, "function");
});
