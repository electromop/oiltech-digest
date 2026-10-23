import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./app/App";
// Порядок слоёв дизайн-системы: токены → база → каркас → компоненты → правила экранов.
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/shell.css";
import "./styles/components.css";
import "./styles/globals.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
