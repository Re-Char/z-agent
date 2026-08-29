import { useEffect, useMemo, useRef } from "react";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import "highlight.js/styles/github-dark-dimmed.css";
import { marked } from "marked";

const LANGUAGES = {
  bash, c, cpp, css, go, java, javascript, json, markdown, python, rust, sql, typescript, xml, yaml,
};
for (const [name, grammar] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, grammar);

marked.setOptions({ gfm: true, breaks: true });

/** Safe GFM renderer with external-link hardening and structured code blocks. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const html = useMemo(() => {
    const rendered = marked.parse(text, { async: false }) as string;
    return DOMPurify.sanitize(rendered, { ADD_ATTR: ["target"] });
  }, [text]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const attach = () => {
      for (const link of root.querySelectorAll<HTMLAnchorElement>("a[href]")) {
        if (/^https?:\/\//i.test(link.href)) {
          link.target = "_blank";
          link.rel = "noreferrer noopener";
        }
      }
      for (const table of root.querySelectorAll("table")) {
        if (table.parentElement?.classList.contains("table-scroll")) continue;
        const wrapper = document.createElement("div");
        wrapper.className = "table-scroll";
        table.parentNode?.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      }
      for (const pre of root.querySelectorAll("pre")) {
        const code = pre.querySelector("code");
        const languageClass = Array.from(code?.classList || []).find((item) => item.startsWith("language-"));
        const language = languageClass?.slice("language-".length) || "代码";
        if (code && !code.dataset.highlighted) {
          const source = code.textContent || "";
          const highlighted = languageClass && hljs.getLanguage(language)
            ? hljs.highlight(source, { language, ignoreIllegals: true })
            : hljs.highlightAuto(source);
          code.innerHTML = highlighted.value;
          code.classList.add("hljs");
          code.dataset.highlighted = "yes";
        }
        if (pre.parentElement?.classList.contains("code-block")) continue;
        const wrapper = document.createElement("div");
        wrapper.className = "code-block";
        const toolbar = document.createElement("div");
        toolbar.className = "code-toolbar";
        const label = document.createElement("span");
        label.textContent = language;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "code-copy";
        button.textContent = "复制";
        button.setAttribute("aria-label", `复制 ${language} 代码`);
        button.addEventListener("click", async (event) => {
          event.stopPropagation();
          const source = code ? code.textContent || "" : pre.textContent || "";
          try {
            await navigator.clipboard.writeText(source);
          } catch (_) {
            // Clipboard API can be unavailable in restricted contexts; fall back.
            const textarea = document.createElement("textarea");
            textarea.value = source;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand("copy");
            textarea.remove();
          }
          button.textContent = "已复制";
          button.classList.add("copied");
          window.setTimeout(() => {
            button.textContent = "复制";
            button.classList.remove("copied");
          }, 1400);
        });
        toolbar.append(label, button);
        pre.parentNode?.insertBefore(wrapper, pre);
        wrapper.append(toolbar, pre);
      }
    };
    attach();
    // markdown -> innerHTML happened before this effect; re-attach after paint
    const frame = requestAnimationFrame(attach);
    return () => cancelAnimationFrame(frame);
  }, [html]);

  return <div ref={rootRef} className={`markdown${className ? ` ${className}` : ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
