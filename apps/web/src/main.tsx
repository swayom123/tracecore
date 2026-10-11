import React from "react";
import { createRoot } from "react-dom/client";
import TraceExplorer from "./TraceExplorer";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <TraceExplorer />
  </React.StrictMode>,
);
