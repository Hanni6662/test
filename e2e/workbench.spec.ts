import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("no-AI project workflow reaches save, export, diff and restore", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "项目", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新建项目" }).click();
  await expect(page.getByRole("dialog", { name: "新建项目" })).toHaveAttribute("aria-modal", "true");
  const projectName = page.getByLabel("项目名称");
  await expect(projectName).toBeFocused();
  await projectName.fill("暂存名称");
  await projectName.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "创建并打开" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "新建项目" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "新建项目" })).toBeFocused();
  await page.getByRole("button", { name: "新建项目" }).click();
  await page.getByLabel("项目名称").fill("夏日童装");
  await page.getByRole("button", { name: "创建并打开" }).click();

  await expect(page.getByRole("heading", { name: "产品档案" })).toBeVisible();
  const uploadInput = page.getByLabel("批量上传参考图");
  await uploadInput.focus();
  await expect(uploadInput).toBeFocused();
  await expect(page.locator(".upload")).toHaveCSS("outline-style", "solid");
  await page.getByLabel("产品标题").fill("女童夏日连衣裙");
  await page.getByLabel("品类").fill("儿童连衣裙");
  await page.getByLabel("受众").fill("4-8 岁女童");
  await page.getByLabel("材质").fill("100% 棉");
  await page.getByLabel("颜色").fill("珊瑚粉, 米白");
  await page.getByLabel("已验证特征").fill("荷叶袖, 收腰");
  await page.getByRole("button", { name: "保存人工档案" }).click();
  await expect(page.getByText("产品档案已保存")).toBeVisible();
  const projectsResponse = await page.request.get("/api/projects");
  const [createdProject] = await projectsResponse.json();
  await page.request.post(`/api/projects/${createdProject.id}/product-profile`, { data: { facts: {
    title: "同步后的女童夏日连衣裙", category: "儿童连衣裙", audience: "4-8 岁女童", material: "100% 棉",
    colors: ["珊瑚粉", "米白"], features: ["荷叶袖", "收腰"], backViewEvidence: false,
  } } });
  await uploadInput.setInputFiles({
    name: "sync.png", mimeType: "image/png",
    buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"),
  });
  await expect(page.getByText("参考图已上传")).toBeVisible();
  await expect(page.getByLabel("产品标题")).toHaveValue("同步后的女童夏日连衣裙");
  await page.getByRole("button", { name: "AI 分类" }).click();
  await expect(page.getByText("未配置 AI，仍可继续人工编辑")).toBeVisible();

  await page.getByRole("button", { name: "单图搭建" }).click();
  await expect(page.getByRole("heading", { name: "模块库" })).toBeVisible();
  await page.getByPlaceholder("搜索模块").fill("背面");
  await page.getByRole("checkbox", { name: "Back view" }).check();
  await expect(page.getByRole("button", { name: "保存不可变版本" })).toBeDisabled();
  await expect(page.getByText("缺少背面图证据", { exact: true })).toBeVisible();
  await page.getByRole("checkbox", { name: "Back view" }).uncheck();
  await page.getByPlaceholder("搜索模块").fill("");
  await page.getByRole("checkbox", { name: /^Amazon listing Amazon/ }).check();
  await page.getByRole("checkbox", { name: /^Amazon listing specification Amazon/ }).check();
  await expect(page.getByLabel("参数 width")).toBeVisible();
  await expect(page.getByLabel("参数 metadata")).toBeVisible();
  await expect(page.getByLabel("参数 height")).toHaveCount(0);
  await page.getByLabel("参数 width").fill("1200");
  await page.getByLabel("参数 metadata").fill('{"label":"hero"}');
  await page.getByRole("checkbox", { name: "Pure white background" }).check();
  await page.getByLabel("场景自定义方向").fill("保持画面留白，商品居中");
  await expect(page.getByText("草稿已自动保存")).toBeVisible();
  await page.getByRole("button", { name: "复制英文" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("Amazon listing");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 JSON" }).click();
  const draftDownload = await downloadPromise;
  expect(draftDownload.suggestedFilename()).toContain("design");
  const draftPath = await draftDownload.path();
  expect(draftPath).not.toBeNull();
  const draft = JSON.parse(await readFile(draftPath!, "utf8"));
  expect(draft.parameters).toEqual({ width: 1200, metadata: { label: "hero" } });
  await page.getByRole("button", { name: "保存不可变版本" }).click();
  await expect(page.getByText("已保存版本 1")).toBeVisible();
  await page.getByRole("checkbox", { name: "Garden party" }).check();
  await expect(page.getByText("草稿已自动保存")).toBeVisible();
  await page.getByRole("button", { name: "保存不可变版本" }).click();
  await expect(page.getByText("已保存版本 2")).toBeVisible();

  await page.getByRole("button", { name: "历史版本" }).click();
  await expect(page.getByRole("button", { name: /^版本 1 / })).toBeVisible();
  await page.getByRole("button", { name: /^版本 1 / }).click();
  const versionDownloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出当前版本" }).click();
  const versionPath = await (await versionDownloadPromise).path();
  expect(versionPath).not.toBeNull();
  const versionExport = JSON.parse(await readFile(versionPath!, "utf8"));
  expect(versionExport.parameters).toEqual({ width: 1200, metadata: { label: "hero" } });
  await page.getByRole("button", { name: "恢复为新版本" }).click();
  await expect(page.getByRole("button", { name: /^版本 3 / })).toBeVisible();
  await page.getByLabel("对比版本 1").selectOption({ label: "版本 1" });
  await page.getByLabel("对比版本 2").selectOption({ label: "版本 2" });
  await expect(page.getByRole("heading", { name: "版本差异" })).toBeVisible();
  await page.getByRole("button", { name: "单图搭建" }).click();
  await expect(page.getByRole("checkbox", { name: "Garden party" })).not.toBeChecked();
});

test("module management separates builtins and custom modules", async ({ page }) => {
  const moduleId = `custom_listing_${Date.now()}`;
  const moduleLabel = `自定义 Listing ${Date.now()}`;
  await page.goto("/");
  await page.getByRole("button", { name: "模块管理" }).click();
  await expect(page.getByRole("heading", { name: "内置模块（只读）" })).toBeVisible();
  await page.getByRole("button", { name: "克隆 Amazon listing specification", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "克隆内置模块" })).toHaveAttribute("aria-modal", "true");
  await page.getByLabel("模块 ID").fill(moduleId);
  await page.getByLabel("模块名称").fill(moduleLabel);
  await page.getByRole("button", { name: "确认克隆" }).click();
  await expect(page.getByText(moduleLabel, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: `编辑 ${moduleLabel}` }).click();
  await expect(page.getByLabel("导出参数键")).toHaveValue("width, metadata");
  await page.getByLabel("导出参数键").fill("width, metadata, quality");
  await page.getByLabel("模块内容").fill("Custom listing direction for e2e.");
  await page.getByRole("button", { name: "保存模块" }).click();
  await expect(page.getByText("自定义模块已保存")).toBeVisible();
  await page.getByRole("button", { name: `编辑 ${moduleLabel}` }).click();
  await expect(page.getByLabel("导出参数键")).toHaveValue("width, metadata, quality");
  await page.getByRole("button", { name: "关闭" }).click();
  await page.getByRole("button", { name: `停用 ${moduleLabel}` }).click();
  await expect(page.locator(".table-row").filter({ hasText: moduleLabel }).getByText("已停用", { exact: true })).toBeVisible();
});

test("settings never renders an API key field or stored secret", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "设置" }).click();
  await expect(page.getByRole("heading", { name: "AI 兼容设置" })).toBeVisible();
  await expect(page.getByText("OPENAI_COMPAT_API_KEY")).toBeVisible();
  await expect(page.getByText("OPENAI_COMPAT_ALLOWED_ORIGINS")).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.getByLabel(/API Key/i)).toHaveCount(0);
});
