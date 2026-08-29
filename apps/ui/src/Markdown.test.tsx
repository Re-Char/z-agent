import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("renders fenced code with language toolbar and copy action", async () => {
    const { container } = render(<Markdown text={"```python\nprint('你好')\n```"} />);
    await waitFor(() => expect(container.querySelector(".code-block")).toBeInTheDocument());
    expect(screen.getByText("python")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 python 代码" })).toBeInTheDocument();
    expect(container.querySelector("code.language-python")?.textContent).toContain("print('你好')");
    expect(container.querySelector("code.language-python")).toHaveClass("hljs");
    expect(container.querySelector("code.language-python .hljs-built_in")).toHaveTextContent("print");
  });

  it("auto-highlights unlabeled code without changing its source text", async () => {
    const { container } = render(<Markdown text={"```\nconst answer = true;\n```"} />);
    await waitFor(() => expect(container.querySelector("code.hljs")).toBeInTheDocument());
    expect(container.querySelector("code.hljs")?.textContent).toBe("const answer = true;\n");
    expect(container.querySelector("code.hljs")?.innerHTML).not.toBe("const answer = true;\n");
  });

  it("sanitizes unsafe markup and wraps wide tables", async () => {
    const { container } = render(<Markdown text={"<script>alert(1)</script>\n\n[外链](https://example.com)\n\n| A | B |\n|---|---|\n| 1 | 2 |"} />);
    expect(container.querySelector("script")).not.toBeInTheDocument();
    await waitFor(() => expect(container.querySelector(".table-scroll table")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "外链" })).toHaveAttribute("rel", "noreferrer noopener");
  });
});
