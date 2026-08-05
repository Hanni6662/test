import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "商图工场 · 跨境电商提示词", description: "从产品事实到可审查提示词的专业工作台。" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="zh-CN"><body>{children}</body></html>; }
