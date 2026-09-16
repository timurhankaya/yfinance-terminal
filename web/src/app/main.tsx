import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { AppRoutes } from "./App";
import { registerAll } from "../panels";
// Imported so Vite emits it as a file, which `style-src 'self'` allows.
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
