import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { AppRoutes } from "./App";
import { registerAll } from "../panels";
// dockview stopped injecting its own stylesheet in v6; imported here it
// becomes a file Vite emits, which `style-src 'self'` allows.
import "dockview/dist/styles/dockview.css";
import "./styles.css";

registerAll();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  </StrictMode>,
);
