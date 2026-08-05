export default function Home() {
  return (
    <main className="shell">
      <header className="hero">
        <p className="eyebrow">Commerce Image Studio</p>
        <h1>跨境电商视觉提示词工作台</h1>
        <p className="lede">
          从真实产品事实出发，组合可复用设计模块，生成结构稳定、可检查的图片提示词。
        </p>
      </header>

      <section className="workspace" aria-label="工作台预览">
        <div>
          <span className="step">01</span>
          <h2>产品事实</h2>
          <p>固定材质、颜色、版型和已验证细节，避免生成内容偏离商品。</p>
        </div>
        <div>
          <span className="step">02</span>
          <h2>设计模块</h2>
          <p>按用途、构图、场景、风格和规格选择视觉方向。</p>
        </div>
        <div>
          <span className="step">03</span>
          <h2>编译结果</h2>
          <p>输出固定八章节提示词，并在生成前提示冲突与证据缺口。</p>
        </div>
      </section>
    </main>
  );
}

