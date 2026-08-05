import { expect, test } from "@playwright/test";

test("no-AI project workflow reaches save, export, diff and restore", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "项目", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新建项目" }).click();
  await page.getByLabel("项目名称").fill("夏日童装");
  await page.getByRole("button", { name: "创建并打开" }).click();

  await expect(page.getByRole("heading", { name: "产品档案" })).toBeVisible();
  await page.getByLabel("产品标题").fill("女童夏日连衣裙");
  await page.getByLabel("品类").fill("儿童连衣裙");
  await page.getByLabel("受众").fill("4-8 岁女童");
  await page.getByLabel("材质").fill("100% 棉");
  await page.getByLabel("颜色").fill("珊瑚粉, 米白");
  await page.getByLabel("已验证特征").fill("荷叶袖, 收腰");
  await page.getByRole("button", { name: "保存人工档案" }).click();
  await expect(page.getByText("产品档案已保存")).toBeVisible();
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
  await page.getByRole("checkbox", { name: "Pure white background" }).check();
  await page.getByLabel("场景自定义方向").fill("保持画面留白，商品居中");
  await expect(page.getByText("草稿已自动保存")).toBeVisible();
  await page.getByRole("button", { name: "复制英文" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain("Amazon listing");
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 JSON" }).click();
  expect((await downloadPromise).suggestedFilename()).toContain("design");
  await page.getByRole("button", { name: "保存不可变版本" }).click();
  await expect(page.getByText("已保存版本 1")).toBeVisible();
  await page.getByRole("checkbox", { name: "Garden party" }).check();
  await expect(page.getByText("草稿已自动保存")).toBeVisible();
  await page.getByRole("button", { name: "保存不可变版本" }).click();
  await expect(page.getByText("已保存版本 2")).toBeVisible();

  await page.getByRole("button", { name: "历史版本" }).click();
  await expect(page.getByRole("button", { name: /^版本 1 / })).toBeVisible();
  await page.getByRole("button", { name: /^版本 1 / }).click();
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
  await page.getByRole("button", { name: "克隆 Amazon listing", exact: true }).click();
  await page.getByLabel("模块 ID").fill(moduleId);
  await page.getByLabel("模块名称").fill(moduleLabel);
  await page.getByRole("button", { name: "确认克隆" }).click();
  await expect(page.getByText(moduleLabel, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: `编辑 ${moduleLabel}` }).click();
  await page.getByLabel("模块内容").fill("Custom listing direction for e2e.");
  await page.getByRole("button", { name: "保存模块" }).click();
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
