import { useEffect, useMemo, useRef, useState } from "react";
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
import "highlight.js/styles/github.css";
import { marked, Renderer, Tokens } from "marked";

const LANGUAGES = {
  bash, c, cpp, css, go, java, javascript, json, markdown, python, rust, sql, typescript, xml, yaml,
};
for (const [name, grammar] of Object.entries(LANGUAGES)) hljs.registerLanguage(name, grammar);

marked.setOptions({ gfm: true, breaks: true });

function normalizedLanguage(value?: string) {
  return (value || "text").split(/\s+/)[0].toLowerCase().replace(/[^a-z0-9_-]/g, "") || "text";
}

function highlightedCode(source: string, language: string) {
  return hljs.getLanguage(language)
    ? hljs.highlight(source, { language, ignoreIllegals: true }).value
    : hljs.highlightAuto(source).value;
}

class MarkdownRenderer extends Renderer {
  code({ text, lang }: Tokens.Code) {
    const language = normalizedLanguage(lang);
    const label = language === "text" ? "代码" : language;
    const source = text.endsWith("\n") ? text : `${text}\n`;
    return `<div class="code-block"><div class="code-toolbar"><span>${label}</span>`
      + `<button type="button" class="code-copy" aria-label="复制 ${label} 代码">复制</button></div>`
      + `<pre><code class="hljs language-${language}" data-highlighted="yes">${highlightedCode(source, language)}</code></pre></div>\n`;
  }

  table(token: Tokens.Table) {
    return `<div class="table-scroll">${super.table(token)}</div>`;
  }
}

const markdownRenderer = new MarkdownRenderer();

const EXTENSION_LANGUAGES: Record<string, string> = {
  ".bash": "bash", ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".css": "css",
  ".go": "go", ".h": "c", ".hpp": "cpp", ".htm": "xml", ".html": "xml",
  ".java": "java", ".js": "javascript", ".jsx": "javascript", ".json": "json",
  ".md": "markdown", ".py": "python", ".rs": "rust", ".sh": "bash", ".sql": "sql",
  ".ts": "typescript", ".tsx": "typescript", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
};

export function languageFromPath(path?: string) {
  if (!path) return "text";
  const filename = path.toLowerCase().split(/[\\/]/).pop() || "";
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? EXTENSION_LANGUAGES[filename.slice(dot)] || "text" : "text";
}

async function copyText(source: string) {
  try {
    await navigator.clipboard.writeText(source);
  } catch (_) {
    const textarea = document.createElement("textarea");
    textarea.value = source;
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }
}

/** Shared renderer for tool payloads and Markdown code fences. */
export function CodeBlock({ code, language = "text", label }: {
  code: string; language?: string; label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const languageName = normalizedLanguage(language);
  const highlighted = useMemo(() => highlightedCode(code, languageName), [code, languageName]);

  async function copy() {
    await copyText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  return <div className="code-block tool-code-block">
    <div className="code-toolbar">
      <span>{label || languageName}</span>
      <button type="button" className={`code-copy${copied ? " copied" : ""}`} aria-label={`复制 ${label || languageName} 代码`} onClick={copy}>
        {copied ? "已复制" : "复制"}
      </button>
    </div>
    <pre><code className={`hljs language-${languageName}`} data-highlighted="yes" dangerouslySetInnerHTML={{ __html: highlighted }} /></pre>
  </div>;
}

/** Safe GFM renderer with external-link hardening and structured code blocks. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const html = useMemo(() => {
    const rendered = marked.parse(text, { async: false, renderer: markdownRenderer }) as string;
    return DOMPurify.sanitize(rendered, { ADD_ATTR: ["target"] });
  }, [text]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    for (const link of root.querySelectorAll<HTMLAnchorElement>("a[href]")) {
      if (/^https?:\/\//i.test(link.href)) {
        link.target = "_blank";
        link.rel = "noreferrer noopener";
      }
    }
    const cleanups: Array<() => void> = [];
    for (const button of root.querySelectorAll<HTMLButtonElement>(".code-copy")) {
      const listener = async (event: MouseEvent) => {
        event.stopPropagation();
        const source = button.closest(".code-block")?.querySelector("code")?.textContent || "";
        await copyText(source);
        button.textContent = "已复制";
        button.classList.add("copied");
        window.setTimeout(() => {
          button.textContent = "复制";
          button.classList.remove("copied");
        }, 1400);
      };
      button.addEventListener("click", listener);
      cleanups.push(() => button.removeEventListener("click", listener));
    }
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [html]);

  return <div ref={rootRef} className={`markdown${className ? ` ${className}` : ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
