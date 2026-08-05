import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "跨境电商视觉提示词工作台",
  description: "将产品事实与可复用设计模块编译为稳定的生成提示词。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

