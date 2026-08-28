import { useEffect, useMemo, useRef } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";

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
        if (pre.parentElement?.classList.contains("code-block")) continue;
        const code = pre.querySelector("code");
        const languageClass = Array.from(code?.classList || []).find((item) => item.startsWith("language-"));
        const language = languageClass?.slice("language-".length) || "代码";
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
