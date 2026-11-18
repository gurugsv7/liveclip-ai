import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import { useStore } from "./lib/store";

// dev-only handle for inspecting state from the browser console
if (import.meta.env.DEV) (window as any).__lcStore = useStore;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
