import { useEffect, useMemo, useRef } from "react";
import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

/** Safe markdown renderer: marked -> DOMPurify -> innerHTML, with copy buttons on code blocks. */
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
      for (const pre of root.querySelectorAll("pre")) {
        if (pre.querySelector(".code-copy")) continue;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "code-copy";
        button.textContent = "复制";
        button.addEventListener("click", async (event) => {
          event.stopPropagation();
          const code = pre.querySelector("code");
          const source = code ? code.innerText : pre.innerText;
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
        pre.appendChild(button);
      }
    };
    attach();
    // markdown -> innerHTML happened before this effect; re-attach after paint
    const frame = requestAnimationFrame(attach);
    return () => cancelAnimationFrame(frame);
  }, [html]);

  return <div ref={rootRef} className={className || "markdown"} dangerouslySetInnerHTML={{ __html: html }} />;
}
