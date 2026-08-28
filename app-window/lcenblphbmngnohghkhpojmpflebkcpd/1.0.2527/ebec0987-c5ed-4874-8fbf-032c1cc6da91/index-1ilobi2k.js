//#region ../../apis/debug.ts
/**
* Public.
*/
function isDebugEnabled() {
	const debug = document.body?.dataset?.debug;
	return debug === "" || debug === "true";
}
//#endregion
//#region ../../apis/log.ts
/**
* Private.
*/
var debugEnabled = null;
/**
* Public.
*/
function debugLog(...args) {
	if (debugEnabled === null) debugEnabled = isDebugEnabled();
	if (debugEnabled) console.debug(...args);
}
//#endregion
//#region ../../apis/locale.ts
/**
* Private.
*/
function parseBcp47LanguageTag(tag) {
	try {
		const [canonicalLocale] = Intl.getCanonicalLocales(tag.trim());
		if (!canonicalLocale?.trim()) return null;
		const { language, region } = new Intl.Locale(canonicalLocale);
		return {
			language,
			region: region || void 0
		};
	} catch {
		return null;
	}
}
function formatLocale(language, region) {
	return `${language.toLowerCase()}-${region.toUpperCase()}`;
}
function findFirstLocaleWithRegion() {
	for (const languageTag of navigator.languages) {
		const bcp47 = parseBcp47LanguageTag(languageTag);
		if (bcp47?.region) return {
			language: bcp47.language,
			region: bcp47.region
		};
	}
	return null;
}
function primaryLanguage() {
	const [primaryLanguageTag] = navigator.languages;
	return parseBcp47LanguageTag(primaryLanguageTag ?? navigator.language)?.language;
}
/**
* Public.
*/
var locale = (() => {
	const localeWithRegion = findFirstLocaleWithRegion();
	if (localeWithRegion) {
		const { language, region } = localeWithRegion;
		return {
			tag: formatLocale(language, region),
			language,
			region
		};
	}
	const language = primaryLanguage();
	return {
		tag: language,
		language,
		region: void 0
	};
})();
//#endregion
//#region ../../house/brave_search/shared/src/locale.ts
/**
* Private.
*/
var localeForDebugging_ = null;
function locale_() {
	return localeForDebugging_ ?? locale.tag ?? null;
}
function languageCode_(locale) {
	try {
		return new Intl.Locale(locale).language;
	} catch {
		debugLog(`Invalid locale: "${locale}"`);
		return null;
	}
}
function regionCode_(locale) {
	try {
		return new Intl.Locale(locale).region ?? null;
	} catch {
		debugLog(`Invalid locale: "${locale}"`);
		return null;
	}
}
function parseLocaleJson_(json) {
	try {
		const parsedJson = JSON.parse(json);
		if (typeof parsedJson !== "object" || parsedJson === null || Array.isArray(parsedJson)) return null;
		if (Object.values(parsedJson).some((value) => typeof value !== "string")) return null;
		return parsedJson;
	} catch {
		debugLog(`Invalid JSON: "${json}"`);
		return null;
	}
}
function findMatchingKey_(localeKeys, locale) {
	if (localeKeys.includes(locale)) return locale;
	const languageCode = languageCode_(locale);
	if (!languageCode) return null;
	if (localeKeys.includes(languageCode)) return languageCode;
	return localeKeys.find((key) => key.startsWith(`${languageCode}-`)) ?? null;
}
function matchLocale_(localeKeys, locale) {
	if (!regionCode_(locale)) return null;
	if (localeKeys.includes(locale)) return locale;
	return null;
}
function matchByRegion_(localeKeys, locale) {
	const regionCode = regionCode_(locale);
	if (!regionCode) return null;
	const languageCode = languageCode_(locale);
	if (languageCode && localeKeys.includes(languageCode)) {
		debugLog(`Region match skipped: language-only key "${languageCode}" covers the region`);
		return null;
	}
	const enLanguageKey = `en-${regionCode}`;
	if (localeKeys.includes(enLanguageKey)) {
		debugLog(`Region match: ${enLanguageKey} (region ${regionCode}, English preferred)`);
		return enLanguageKey;
	}
	const regionMatch = localeKeys.find((key) => key.endsWith(`-${regionCode}`)) ?? null;
	if (regionMatch) debugLog(`Region match: ${regionMatch} (region ${regionCode})`);
	return regionMatch;
}
function matchByLanguage_(localeKeys, locale) {
	const languageCode = languageCode_(locale);
	if (!languageCode) return null;
	if (localeKeys.includes(languageCode)) {
		debugLog(`Language-only match: ${languageCode}`);
		return languageCode;
	}
	for (const language of navigator.languages) {
		const match = findMatchingKey_(localeKeys, language);
		if (match?.startsWith(`${languageCode}-`)) {
			debugLog(`Navigator language ${language} matched content key ${match}`);
			return match;
		}
	}
	const contentMatch = localeKeys.find((key) => key.startsWith(`${languageCode}-`)) ?? null;
	if (contentMatch) debugLog(`Language content match: ${contentMatch}`);
	return contentMatch;
}
function matchFallback_(localeKeys) {
	for (const language of [...navigator.languages, "en"]) {
		const match = findMatchingKey_(localeKeys, language);
		if (match) {
			debugLog(`Fallback: ${language === "en" ? "English" : `navigator language ${language}`} matched content key ${match} (${locale_()})`);
			return match;
		}
	}
	debugLog(`Fallback: no match found, using first content key "${localeKeys[0]}"`);
	return localeKeys[0];
}
function resolveMatches_(localeKeys) {
	const locale = locale_();
	if (locale === null) {
		debugLog("No locale available, using fallback");
		return {
			localeMatch: null,
			broadMatch: matchFallback_(localeKeys)
		};
	}
	const languageCode = languageCode_(locale);
	const regionCode = regionCode_(locale);
	debugLog(`Resolving matches for locale ${locale}`);
	let localeMatch = matchLocale_(localeKeys, locale);
	if (localeMatch === null) debugLog(`Step 1 no match: ${locale}`);
	else debugLog(`Step 1 matched: ${localeMatch}`);
	if (localeMatch === null) {
		localeMatch = matchByRegion_(localeKeys, locale);
		if (localeMatch === null) debugLog(`Step 2 no match: ${regionCode ? `xx-${regionCode}` : "no region"} (${locale})`);
		else debugLog(`Step 2 matched: ${localeMatch}`);
	} else debugLog(`Step 2 skipped: step 1 matched ${localeMatch}`);
	let broadMatch = matchByLanguage_(localeKeys, locale);
	if (broadMatch === null) debugLog(`Step 3 no match: language ${languageCode ?? "xx"} (${locale})`);
	else debugLog(`Step 3 matched: ${broadMatch}`);
	if (broadMatch === null) {
		debugLog(`Step 4 fallback: no language match for ${locale}`);
		broadMatch = matchFallback_(localeKeys);
	}
	return {
		localeMatch,
		broadMatch
	};
}
function setLocaleForDebugging(locale) {
	if (locale === null) {
		localeForDebugging_ = null;
		return;
	}
	try {
		localeForDebugging_ = new Intl.Locale(locale).baseName;
		debugLog(`Overridden locale: ${localeForDebugging_}`);
	} catch {
		debugLog(`Invalid overridden locale: ${locale}, falling back to browser locale`);
		localeForDebugging_ = null;
	}
}
function localizeContent(localeMap) {
	if (!localeMap) {
		debugLog("No locale content provided");
		return [];
	}
	const localeKeys = Object.keys(localeMap);
	if (localeKeys.length === 0) {
		debugLog("Locale content has no entries");
		return [];
	}
	const { localeMatch, broadMatch } = resolveMatches_(localeKeys);
	if (!localeMatch) {
		const localizedContent = localeMap[broadMatch];
		if (localizedContent !== void 0) debugLog(`Localizing content for ${broadMatch}`, localizedContent);
		else debugLog(`No localized content for ${broadMatch}`);
		return localizedContent ?? [];
	}
	if (regionCode_(broadMatch)) {
		const localizedContent = localeMap[localeMatch];
		if (localizedContent !== void 0) debugLog(`Localizing content for ${localeMatch}`, localizedContent);
		else debugLog(`No localized content for ${localeMatch}`);
		return localizedContent ?? [];
	}
	const languageContent = localeMap[broadMatch];
	const regionContent = localeMap[localeMatch];
	const mergedContent = [...languageContent ?? [], ...regionContent ?? []];
	if (mergedContent.length > 0) debugLog(`Localizing content for ${localeMatch} and ${broadMatch}`, mergedContent);
	else debugLog(`No localized content for ${localeMatch} and ${broadMatch}`);
	return mergedContent;
}
function localizeText(json, fallbackText) {
	if (!json) {
		debugLog(`No locale text provided, using: "${fallbackText}"`);
		return fallbackText;
	}
	const localeMap = parseLocaleJson_(json);
	if (!localeMap) {
		debugLog(`Locale text is invalid JSON, using: "${fallbackText}"`);
		return fallbackText;
	}
	const localeKeys = Object.keys(localeMap);
	if (localeKeys.length === 0) {
		debugLog(`Locale text has no entries, using: "${fallbackText}"`);
		return fallbackText;
	}
	const { localeMatch, broadMatch } = resolveMatches_(localeKeys);
	if (localeMatch) {
		const localizedText = localeMap[localeMatch];
		if (localizedText !== void 0) {
			debugLog(`Localizing text for ${localeMatch}: "${localizedText}"`);
			return localizedText;
		}
	}
	const localizedFallbackText = localeMap[broadMatch];
	const text = localizedFallbackText ?? fallbackText;
	debugLog(localizedFallbackText !== void 0 ? `Localizing text for ${broadMatch}: "${text}"` : `No locale match for text, using: "${text}"`);
	return text;
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/state.ts
function getState() {
	try {
		return JSON.parse(window.name);
	} catch {
		return {};
	}
}
function setState(update) {
	window.name = JSON.stringify({
		...getState(),
		...update
	});
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/debug-picker.ts
/**
* Private.
*/
function createButtonElement_(id, label) {
	const buttonElement = document.createElement("button");
	buttonElement.id = `${id}-label`;
	buttonElement.type = "button";
	if (label) buttonElement.appendChild(document.createTextNode(`${label} (`));
	const nameSpanElement = document.createElement("span");
	nameSpanElement.id = `${id}-label-name`;
	buttonElement.appendChild(nameSpanElement);
	const iconSpanElement = document.createElement("span");
	iconSpanElement.id = `${id}-label-icon`;
	iconSpanElement.className = "debug-picker-label-icon";
	iconSpanElement.style.display = "none";
	buttonElement.appendChild(iconSpanElement);
	if (label) buttonElement.appendChild(document.createTextNode(")"));
	return {
		buttonElement,
		setLabelName(text) {
			nameSpanElement.textContent = text;
		},
		setLabelIcon(iconClass) {
			if (iconClass) {
				iconSpanElement.className = `debug-picker-label-icon ${iconClass}`;
				iconSpanElement.style.display = "";
			} else iconSpanElement.style.display = "none";
		}
	};
}
function createOptionsDivElement_(id) {
	const divElement = document.createElement("div");
	divElement.id = `${id}-options`;
	divElement.className = "debug-picker-options";
	divElement.hidden = true;
	return divElement;
}
function createDropdownSpanElement_(id, optionsDivElement) {
	const dropdownSpanElement = document.createElement("span");
	dropdownSpanElement.id = `${id}-dropdown`;
	const chevronSpanElement = document.createElement("span");
	chevronSpanElement.id = `${id}-chevron`;
	dropdownSpanElement.append(chevronSpanElement, optionsDivElement);
	return dropdownSpanElement;
}
function populateOptions_(optionsDivElement, groups) {
	groups.forEach((group) => {
		if ("divider" in group) {
			const dividerDivElement = document.createElement("div");
			dividerDivElement.className = "debug-picker-divider";
			optionsDivElement.appendChild(dividerDivElement);
			return;
		}
		if (group.groupLabel) {
			const groupLabelSpanElement = document.createElement("span");
			groupLabelSpanElement.className = "debug-picker-group-label";
			groupLabelSpanElement.textContent = group.groupLabel;
			optionsDivElement.appendChild(groupLabelSpanElement);
		}
		group.items.forEach(({ label, value, icon, description }) => {
			const displayLabel = label ?? value;
			const optionButtonElement = document.createElement("button");
			optionButtonElement.type = "button";
			optionButtonElement.dataset.value = value;
			if (description) {
				optionButtonElement.textContent = displayLabel;
				const descriptionSpanElement = document.createElement("span");
				descriptionSpanElement.className = "debug-picker-item-description";
				descriptionSpanElement.textContent = ` (${description})`;
				optionButtonElement.appendChild(descriptionSpanElement);
			} else optionButtonElement.textContent = displayLabel;
			if (icon) {
				const iconSpanElement = document.createElement("span");
				iconSpanElement.className = `debug-picker-item-icon ${icon}`;
				optionButtonElement.appendChild(iconSpanElement);
			}
			optionsDivElement.appendChild(optionButtonElement);
		});
	});
}
function createPickerDivElement_(id, buttonElement, dropdownSpanElement) {
	const divElement = document.createElement("div");
	divElement.id = `${id}-picker`;
	divElement.append(buttonElement, dropdownSpanElement);
	return divElement;
}
function setupDropdown_(pickerDivElement, optionsDivElement) {
	const toggle = () => {
		optionsDivElement.hidden = !optionsDivElement.hidden;
		pickerDivElement.classList.toggle("open", !optionsDivElement.hidden);
	};
	pickerDivElement.addEventListener("click", toggle);
	document.addEventListener("pointerdown", (event) => {
		if (event.target instanceof Node && !optionsDivElement.hidden && !pickerDivElement.contains(event.target)) {
			optionsDivElement.hidden = true;
			pickerDivElement.classList.remove("open");
		}
	});
}
/**
* Public.
*/
function createPicker(id, groups, label) {
	const { buttonElement, setLabelName, setLabelIcon } = createButtonElement_(id, label);
	const optionsDivElement = createOptionsDivElement_(id);
	populateOptions_(optionsDivElement, groups);
	const pickerDivElement = createPickerDivElement_(id, buttonElement, createDropdownSpanElement_(id, optionsDivElement));
	setupDropdown_(pickerDivElement, optionsDivElement);
	return {
		divElement: pickerDivElement,
		setLabelName,
		setLabelIcon,
		setSelected(value) {
			optionsDivElement.querySelectorAll("button[data-value]").forEach((optionButtonElement) => {
				if (optionButtonElement.dataset.value === value) optionButtonElement.dataset.active = "";
				else delete optionButtonElement.dataset.active;
			});
		},
		onSelect(callback) {
			optionsDivElement.querySelectorAll("button[data-value]").forEach((optionButtonElement) => {
				optionButtonElement.addEventListener("click", () => {
					callback(optionButtonElement.dataset.value ?? "");
				});
			});
		}
	};
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/locale/index.ts
/**
* Private.
*/
var localePickerGroups = [
	{ items: [{
		value: "auto",
		description: "browser locale"
	}] },
	{
		groupLabel: "Locales",
		items: [
			"en-US",
			"es-US",
			"en-GB",
			"en-CA",
			"fr-CA",
			"fr-FR",
			"de-DE"
		].map((value) => ({ value }))
	},
	{
		groupLabel: "Region",
		items: [
			"xx-US",
			"xx-GB",
			"xx-CA",
			"xx-FR",
			"xx-DE"
		].map((value) => ({ value }))
	},
	{
		groupLabel: "Language",
		items: [
			"en",
			"en-XX",
			"fr",
			"fr-XX",
			"de",
			"de-XX",
			"es",
			"es-XX"
		].map((value) => ({ value }))
	},
	{
		groupLabel: "Fallback",
		items: ["xx", "xx-XX"].map((value) => ({ value }))
	},
	{
		groupLabel: "BCP 47 formats",
		items: [
			{
				value: "es-021",
				description: "UN M.49 Northern America"
			},
			{
				value: "en-Latn",
				description: "Latin script, no region"
			},
			{
				value: "en-Latn-US",
				description: "Latin script, with region"
			},
			{
				value: "de-1901",
				description: "variant, 1901 orthography"
			}
		]
	},
	{
		groupLabel: "Normalization",
		items: [{
			value: "EN-US",
			description: "uppercase"
		}, {
			value: "en-us",
			description: "lowercase"
		}]
	}
];
/**
* Public.
*/
function createLocalePicker() {
	const picker = createPicker("debug-locale", localePickerGroups, "locale");
	return {
		divElement: picker.divElement,
		init() {
			const savedLocale = getState().locale;
			const selectedLocale = savedLocale ?? "auto";
			picker.setLabelName(savedLocale ?? locale.tag ?? "auto");
			picker.setSelected(selectedLocale);
			picker.onSelect((value) => {
				if (value === selectedLocale) return;
				setState({
					locale: value === "auto" ? void 0 : value,
					shouldRestoreSafeAreaOnNextLoad: true
				});
				location.reload();
			});
		}
	};
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/autotype/index.ts
/**
* Private.
*/
var autotypePickerGroups = [
	{ items: [{
		label: "interactive",
		value: "interactive"
	}] },
	{
		groupLabel: "Modes",
		items: [{
			label: "random",
			value: "autotype-random"
		}]
	},
	{ divider: true },
	{ items: [
		{
			label: "assemble",
			value: "autotype-assemble"
		},
		{
			label: "bounce",
			value: "autotype-bounce"
		},
		{
			label: "caret",
			value: "autotype-caret-with-pointer",
			icon: "debug-autotype-icon-mouse-pointer"
		},
		{
			label: "caret",
			value: "autotype-caret"
		},
		{
			label: "fade",
			value: "autotype-fade"
		},
		{
			label: "fade chars",
			value: "autotype-fade-chars"
		},
		{
			label: "focus",
			value: "autotype-focus"
		},
		{
			label: "ghost",
			value: "autotype-ghost"
		},
		{
			label: "magnify",
			value: "autotype-magnify"
		},
		{
			label: "neon",
			value: "autotype-neon"
		},
		{
			label: "redact",
			value: "autotype-redact"
		},
		{
			label: "reveal",
			value: "autotype-reveal"
		},
		{
			label: "scramble",
			value: "autotype-scramble"
		},
		{
			label: "slot machine",
			value: "autotype-slot-machine"
		},
		{
			label: "sprinkle",
			value: "autotype-sprinkle"
		},
		{
			label: "waterfall",
			value: "autotype-waterfall"
		},
		{
			label: "word burst",
			value: "autotype-word-burst"
		}
	] },
	{
		groupLabel: "Accessibility",
		items: [{
			label: "reduced motion",
			value: "autotype-reduced-motion"
		}]
	}
];
function findMode_(modeValue) {
	for (const group of autotypePickerGroups) {
		if ("divider" in group) continue;
		const match = group.items.find((item) => item.value === modeValue);
		if (match) return match;
	}
}
function setModeName_(value, picker) {
	const mode = findMode_(value);
	picker.setLabelName(mode?.label ?? value);
}
function setModeIcon_(value, picker) {
	const mode = findMode_(value);
	picker.setLabelIcon(mode?.icon);
}
/**
* Public.
*/
function setSearchBoxMode(searchBoxElement, mode) {
	if (mode === "autotype-caret") {
		searchBoxElement.dataset.searchMode = "autotype-caret";
		searchBoxElement.dataset.hideMousePointer = "";
	} else if (mode === "autotype-caret-with-pointer") {
		searchBoxElement.dataset.searchMode = "autotype-caret";
		delete searchBoxElement.dataset.hideMousePointer;
	} else searchBoxElement.dataset.searchMode = mode;
}
function getSearchBoxMode_(searchBoxElement) {
	const searchMode = searchBoxElement.dataset.searchMode ?? "";
	const hasHidePointer = searchBoxElement.hasAttribute("data-hide-mouse-pointer");
	if (searchMode === "autotype-caret") return hasHidePointer ? "autotype-caret" : "autotype-caret-with-pointer";
	return searchMode;
}
function createAutotypePicker() {
	const picker = createPicker("debug-autotype", autotypePickerGroups, "autotype");
	return {
		divElement: picker.divElement,
		init() {
			const activeMode = getState().autotypeMode;
			const searchBoxElement = document.querySelector(".search-box");
			const effectiveMode = searchBoxElement ? getSearchBoxMode_(searchBoxElement) : "";
			const initialMode = activeMode ?? effectiveMode ?? "auto";
			setModeName_(initialMode, picker);
			setModeIcon_(initialMode, picker);
			const selectedMode = activeMode ?? effectiveMode;
			picker.setSelected(selectedMode);
			picker.onSelect((value) => {
				if (value === selectedMode) return;
				setState({
					autotypeMode: value || void 0,
					shouldRestoreSafeAreaOnNextLoad: true
				});
				location.reload();
			});
			document.addEventListener("autotype-mode-change", (event) => {
				let mode = event.detail;
				if (mode === "autotype-caret" && !document.querySelector(".search-box")?.hasAttribute("data-hide-mouse-pointer")) mode = "autotype-caret-with-pointer";
				const storedMode = getState().autotypeMode;
				if (!storedMode || storedMode === "autotype-random") {
					setModeName_(mode, picker);
					setModeIcon_(mode, picker);
				}
			});
		}
	};
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/query-order/index.ts
/**
* Private.
*/
var queryOrderPickerGroups = [{ items: ["random", "sequential"].map((order) => ({
	label: order,
	value: order
})) }];
/**
* Public.
*/
function createQueryOrderPicker() {
	const picker = createPicker("debug-query-order", queryOrderPickerGroups, "query order");
	return {
		divElement: picker.divElement,
		init() {
			const defaultQueryOrder = document.querySelector(".search-box")?.dataset.randomizeQueries !== void 0 ? "random" : "sequential";
			const selectedOrder = getState().queryOrder ?? defaultQueryOrder;
			picker.setLabelName(selectedOrder);
			picker.setSelected(selectedOrder);
			picker.onSelect((value) => {
				const order = value;
				if (order === selectedOrder) return;
				setState({
					queryOrder: order,
					shouldRestoreSafeAreaOnNextLoad: true
				});
				location.reload();
			});
		}
	};
}
//#endregion
//#region ../../apis/geometry.ts
function clampRect(rect, containerRect) {
	const clampedX = Math.max(containerRect.x, Math.min(rect.x, containerRect.x + containerRect.width));
	const clampedY = Math.max(containerRect.y, Math.min(rect.y, containerRect.y + containerRect.height));
	return {
		x: clampedX,
		y: clampedY,
		width: Math.max(0, Math.min(rect.width, containerRect.x + containerRect.width - clampedX)),
		height: Math.max(0, Math.min(rect.height, containerRect.y + containerRect.height - clampedY))
	};
}
//#endregion
//#region ../../apis/os.ts
/**
* Public.
*/
var OperatingSystemType = /* @__PURE__ */ function(OperatingSystemType) {
	OperatingSystemType["Android"] = "Android";
	OperatingSystemType["iOS"] = "iOS";
	OperatingSystemType["Windows"] = "Windows";
	OperatingSystemType["Mac"] = "Mac";
	OperatingSystemType["Linux"] = "Linux";
	OperatingSystemType["Unknown"] = "Unknown";
	return OperatingSystemType;
}({});
var operatingSystemType = (() => {
	const userAgentData = navigator.userAgentData;
	if (userAgentData?.platform) switch (userAgentData.platform) {
		case "Android": return "Android";
		case "iOS": return "iOS";
		case "Windows": return "Windows";
		case "macOS": return "Mac";
		case "Linux": return "Linux";
		default: return "Unknown";
	}
	const userAgent = navigator.userAgent;
	if (/android/i.test(userAgent)) return "Android";
	if (/iPad|iPhone|iPod/.test(userAgent)) return "iOS";
	if (/Win/.test(userAgent)) return "Windows";
	if (/Mac/.test(userAgent)) return "Mac";
	if (/Linux/.test(userAgent)) return "Linux";
	return "Unknown";
})();
//#endregion
//#region ../../apis/platform.ts
/**
* Public.
*/
var isMobile = (() => {
	const userAgentData = navigator.userAgentData;
	if (userAgentData?.mobile !== void 0) return userAgentData.mobile;
	return operatingSystemType === OperatingSystemType.Android || operatingSystemType === OperatingSystemType.iOS;
})();
//#endregion
//#region ../../apis/bridge.ts
/**
* Private.
*/
var RICH_MEDIA_EVENT = "richMediaEvent";
function trustedOrigin() {
	return operatingSystemType === OperatingSystemType.Android ? "chrome://new-tab-takeover" : "chrome://newtab";
}
/**
* Public.
*/
function isTrustedOrigin(origin) {
	return origin === trustedOrigin();
}
function postMessage(payload) {
	window.parent.postMessage(payload, trustedOrigin());
}
function postRichMediaEvent(eventType) {
	postMessage({
		type: RICH_MEDIA_EVENT,
		value: eventType
	});
}
//#endregion
//#region ../../apis/safe-area-debug.ts
/**
* Private.
*/
function drawSafeAreaDebugOverlay(rect) {
	const id = "debug-safe-area";
	let debugOverlayElement = document.getElementById(id);
	if (!debugOverlayElement) {
		debugOverlayElement = document.createElement("div");
		debugOverlayElement.id = id;
		debugOverlayElement.style.position = "fixed";
		debugOverlayElement.style.boxSizing = "border-box";
		debugOverlayElement.style.background = "transparent";
		debugOverlayElement.style.border = "4px solid rgba(0, 255, 0, 0.7)";
		debugOverlayElement.style.pointerEvents = "none";
		debugOverlayElement.style.zIndex = "2147483647";
		document.body.appendChild(debugOverlayElement);
	}
	debugOverlayElement.style.left = `${rect.x}px`;
	debugOverlayElement.style.top = `${rect.y}px`;
	debugOverlayElement.style.width = `${rect.width}px`;
	debugOverlayElement.style.height = `${rect.height}px`;
}
/**
* Public.
*/
function maybeDrawSafeAreaDebugOverlay(rect) {
	if (!document.body) {
		document.addEventListener("DOMContentLoaded", () => maybeDrawSafeAreaDebugOverlay(rect), { once: true });
		return;
	}
	if (isDebugEnabled()) drawSafeAreaDebugOverlay(rect);
}
//#endregion
//#region ../../apis/safe-area.ts
/**
* Private.
*/
var currentSafeAreaRect = null;
var isSafeAreaInitialized = false;
var hasSafeAreaFirstLayoutCompleted = false;
var pendingSafeAreaLayoutUpdate = false;
var resolveSafeAreaReady = null;
new Promise((resolve) => {
	resolveSafeAreaReady = resolve;
});
var LEGACY_WIDE_DESKTOP_INSET = {
	top: 128,
	right: 24,
	bottom: 200,
	left: 24
};
var LEGACY_NARROW_DESKTOP_INSET = {
	top: 224,
	right: 24,
	bottom: 200,
	left: 24
};
var LEGACY_MOBILE_INSET = {
	top: 156,
	right: 12,
	bottom: 58,
	left: 12
};
var NARROW_DESKTOP_MAX_WIDTH = 643;
function legacySafeAreaInsets() {
	if (isMobile) return LEGACY_MOBILE_INSET;
	if (window.innerWidth <= NARROW_DESKTOP_MAX_WIDTH) return LEGACY_NARROW_DESKTOP_INSET;
	return LEGACY_WIDE_DESKTOP_INSET;
}
function legacySafeAreaRect() {
	const inset = legacySafeAreaInsets();
	return new DOMRectReadOnly(inset.left, inset.top, window.innerWidth - (inset.left + inset.right), window.innerHeight - (inset.top + inset.bottom));
}
function getSafeAreaRect() {
	return currentSafeAreaRect ?? legacySafeAreaRect();
}
function setSafeAreaCSSVariables(rect) {
	const style = document.documentElement.style;
	const top = rect.y;
	const right = window.innerWidth - rect.right;
	const bottom = window.innerHeight - rect.bottom;
	const left = rect.x;
	style.setProperty("--safe-area-x", `${rect.x}px`);
	style.setProperty("--safe-area-y", `${rect.y}px`);
	style.setProperty("--safe-area-width", `${rect.width}px`);
	style.setProperty("--safe-area-height", `${rect.height}px`);
	style.setProperty("--safe-area-top", `${top}px`);
	style.setProperty("--safe-area-bottom", `${bottom}px`);
	style.setProperty("--safe-area-left", `${left}px`);
	style.setProperty("--safe-area-right", `${right}px`);
	style.setProperty("--safe-area", `${top}px ${right}px ${bottom}px ${left}px`);
	maybeDrawSafeAreaDebugOverlay(rect);
}
function scheduleAfterDom(onReady) {
	if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => requestAnimationFrame(onReady), { once: true });
	else requestAnimationFrame(onReady);
}
function notifySafeAreaLayoutChange() {
	debugLog("Dispatching layoutSafeArea event.");
	window.dispatchEvent(new CustomEvent("layoutSafeArea"));
}
function updateSafeAreaLayout() {
	if (pendingSafeAreaLayoutUpdate) {
		debugLog("Safe area layout update already pending, skipping.");
		return;
	}
	pendingSafeAreaLayoutUpdate = true;
	debugLog("Safe area layout update scheduled. isMobile:", isMobile, "innerWidth:", window.innerWidth, "innerHeight:", window.innerHeight);
	scheduleAfterDom(() => {
		pendingSafeAreaLayoutUpdate = false;
		hasSafeAreaFirstLayoutCompleted = true;
		const usingBridgeRect = currentSafeAreaRect !== null;
		const safeAreaRect = getSafeAreaRect();
		debugLog("Safe area applying. source:", usingBridgeRect ? "bridge" : "legacy", "rect:", safeAreaRect);
		setSafeAreaCSSVariables(safeAreaRect);
		notifySafeAreaLayoutChange();
		resolveSafeAreaReady?.();
	});
}
function subscribeToSafeAreaLayoutChanges() {
	window.addEventListener("message", (messageEvent) => {
		if (!isTrustedOrigin(messageEvent.origin)) return;
		const { type, value } = messageEvent.data ?? {};
		debugLog("Safe area bridge message. type:", type, "origin:", messageEvent.origin);
		if (type === "richMediaSafeRect" && value && typeof value.x === "number" && typeof value.y === "number" && typeof value.width === "number" && typeof value.height === "number") {
			debugLog("Safe area bridge rect received. raw:", value);
			const clamped = clampRect({
				x: value.x,
				y: value.y,
				width: value.width,
				height: value.height
			}, {
				x: 0,
				y: 0,
				width: window.innerWidth,
				height: window.innerHeight
			});
			debugLog("Safe area bridge rect clamped:", clamped);
			currentSafeAreaRect = new DOMRectReadOnly(clamped.x, clamped.y, clamped.width, clamped.height);
			debugLog("Safe area rect", currentSafeAreaRect);
			updateSafeAreaLayout();
		} else if (type !== void 0) debugLog("Safe area bridge message ignored. type:", type, "value:", value);
	});
	window.addEventListener("resize", updateSafeAreaLayout);
}
function initSafeArea() {
	if (isSafeAreaInitialized) return;
	isSafeAreaInitialized = true;
	updateSafeAreaLayout();
	subscribeToSafeAreaLayoutChanges();
}
function registerSafeAreaLayoutHandler(onLayout) {
	if (hasSafeAreaFirstLayoutCompleted) {
		debugLog("Safe area layout already applied, firing handler on next frame.");
		requestAnimationFrame(() => onLayout(getSafeAreaRect()));
	}
	const onLayoutChange = () => {
		debugLog("Safe area layout event received, document state", document.readyState);
		scheduleAfterDom(() => onLayout(getSafeAreaRect()));
	};
	window.addEventListener("layoutSafeArea", onLayoutChange);
	return () => window.removeEventListener("layoutSafeArea", onLayoutChange);
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/safe-area.ts
/**
* Private.
*/
function shouldCaptureIncomingSafeAreaRectangle_() {
	return !getState().rect;
}
function clearCachedSafeAreaState_() {
	setState({
		rect: void 0,
		shouldRestoreSafeAreaOnNextLoad: void 0
	});
}
function isReloadNavigation_() {
	return performance.getEntriesByType("navigation")[0]?.type === "reload";
}
function applySafeAreaRectangle_(rect) {
	const { x, y, width, height } = rect;
	const style = document.documentElement.style;
	const top = y;
	const right = window.innerWidth - (x + width);
	const bottom = window.innerHeight - (y + height);
	const left = x;
	style.setProperty("--safe-area-x", `${x}px`);
	style.setProperty("--safe-area-y", `${y}px`);
	style.setProperty("--safe-area-width", `${width}px`);
	style.setProperty("--safe-area-height", `${height}px`);
	style.setProperty("--safe-area-top", `${top}px`);
	style.setProperty("--safe-area-bottom", `${bottom}px`);
	style.setProperty("--safe-area-left", `${left}px`);
	style.setProperty("--safe-area-right", `${right}px`);
	style.setProperty("--safe-area", `${top}px ${right}px ${bottom}px ${left}px`);
	const safeAreaElement = document.getElementById("debug-safe-area");
	if (safeAreaElement) {
		safeAreaElement.style.left = `${x}px`;
		safeAreaElement.style.top = `${y}px`;
		safeAreaElement.style.width = `${width}px`;
		safeAreaElement.style.height = `${height}px`;
	}
}
/**
* Public.
*/
function initSafeAreaRestore(savedRect, shouldRestore, shouldRestoreSafeAreaOnNextLoad) {
	if (!savedRect || !shouldRestore) return;
	if (shouldRestoreSafeAreaOnNextLoad) setState({ shouldRestoreSafeAreaOnNextLoad: void 0 });
	const applyRect = () => {
		applySafeAreaRectangle_(savedRect);
	};
	requestAnimationFrame(applyRect);
	let isRestoreActive = true;
	let debounceTimeoutId = null;
	registerSafeAreaLayoutHandler(() => {
		if (!isRestoreActive) return;
		if (debounceTimeoutId !== null) clearTimeout(debounceTimeoutId);
		debounceTimeoutId = setTimeout(() => {
			debounceTimeoutId = null;
			applyRect();
			isRestoreActive = false;
		});
	});
}
function handleIncomingSafeAreaRectangleMessage(event) {
	const { type, value } = event.data ?? {};
	if (type !== "richMediaSafeRect" || !value) return;
	if (!shouldCaptureIncomingSafeAreaRectangle_()) return;
	setState({ rect: value });
}
function handleViewportResize() {
	clearCachedSafeAreaState_();
}
function getSafeAreaRestoreConfig() {
	const { rect, shouldRestoreSafeAreaOnNextLoad } = getState();
	return {
		rect,
		shouldRestore: Boolean(rect) && (shouldRestoreSafeAreaOnNextLoad === true || isReloadNavigation_()),
		shouldRestoreSafeAreaOnNextLoad
	};
}
//#endregion
//#region ../../house/brave_search/shared/src/debug/debug-bar.ts
/**
* Private.
*/
var isInitialized_$1 = false;
var barElement_ = null;
function createSeparatorSpanElement_() {
	const spanElement = document.createElement("span");
	spanElement.className = "debug-bar-separator";
	spanElement.textContent = "·";
	return spanElement;
}
/**
* Public.
*/
function initBraveSearchDebugBar() {
	if (isInitialized_$1) return;
	if (!(document.querySelector(".search-box")?.hasAttribute("data-show-debug-bar") ?? false)) return;
	isInitialized_$1 = true;
	barElement_ = document.createElement("div");
	barElement_.id = "debug-bar";
	document.body.appendChild(barElement_);
	const localePicker = createLocalePicker();
	const autotypePicker = createAutotypePicker();
	const queryOrderPicker = createQueryOrderPicker();
	barElement_.append(localePicker.divElement, createSeparatorSpanElement_(), autotypePicker.divElement, createSeparatorSpanElement_(), queryOrderPicker.divElement);
	const { locale: lastLocale, autotypeMode: lastAutotypeMode, queryOrder: lastQueryOrder } = getState();
	if (lastLocale) setLocaleForDebugging(lastLocale);
	if (lastAutotypeMode || lastQueryOrder) {
		const searchBoxElement = document.querySelector(".search-box");
		if (searchBoxElement) {
			if (lastAutotypeMode) setSearchBoxMode(searchBoxElement, lastAutotypeMode);
			if (lastQueryOrder === "random") searchBoxElement.dataset.randomizeQueries = "";
			else if (lastQueryOrder === "sequential") delete searchBoxElement.dataset.randomizeQueries;
		}
	}
	const { rect, shouldRestore, shouldRestoreSafeAreaOnNextLoad: restoreFlag } = getSafeAreaRestoreConfig();
	initSafeAreaRestore(rect, shouldRestore, restoreFlag);
	window.addEventListener("message", handleIncomingSafeAreaRectangleMessage);
	window.addEventListener("resize", handleViewportResize);
	localePicker.init();
	autotypePicker.init();
	queryOrderPicker.init();
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initBraveSearchDebugBar);
else initBraveSearchDebugBar();
//#endregion
//#region ../../apis/event-binder.ts
/**
* Private.
*/
function bindClickToSelectors(selectors, onElement) {
	[selectors].flat().forEach((selector) => {
		const elements = document.querySelectorAll(selector);
		if (elements.length === 0) {
			console.warn(`No elements found for selector ${selector}.`);
			return;
		}
		elements.forEach(onElement);
	});
}
//#endregion
//#region ../../apis/event-dispatcher.ts
/**
* Private.
*/
var dispatchedEvents = /* @__PURE__ */ new Set();
/**
* Public.
*/
var richMediaEventTypes = {
	CLICK: "click",
	INTERACTION: "interaction",
	MEDIA_PLAY: "mediaPlay",
	MEDIA_25: "media25",
	MEDIA_100: "media100"
};
function dispatchRichMediaEvent(eventType) {
	if (dispatchedEvents.has(eventType)) {
		debugLog(`${eventType} event already dispatched, skipping.`);
		return;
	}
	dispatchedEvents.add(eventType);
	debugLog(`Dispatching ${eventType} event.`);
	postRichMediaEvent(eventType);
}
function bindAndDispatchClickEvent(selectors) {
	bindClickToSelectors(selectors, (element) => element.addEventListener("click", () => dispatchRichMediaEvent(richMediaEventTypes.CLICK)));
}
//#endregion
//#region ../../apis/wallpaper/script.ts
function initWallpaper() {
	initSafeArea();
	document.addEventListener("contextmenu", (event) => event.preventDefault());
	bindAndDispatchClickEvent("img.wallpaper");
	function setFocalPoints() {
		const wallpaper = document.querySelector(".wallpaper");
		if (!wallpaper) {
			console.warn("Wallpaper not found, failed to initialize.");
			return;
		}
		wallpaper.style.objectPosition = wallpaper.dataset.focalPoint || "center";
	}
	setFocalPoints();
}
//#endregion
//#region ../../apis/accessibility.ts
/**
* Public.
*/
var prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
var prefersReducedTransparency = window.matchMedia("(prefers-reduced-transparency: reduce)").matches;
//#endregion
//#region ../../apis/browser.ts
/**
* Private.
*/
var majorBrowserVersion = (() => {
	const match = navigator.userAgent.match(/Chrome\/(\d+)/);
	if (!match) return null;
	const [, browserVersion] = match;
	return parseInt(browserVersion, 10);
})();
/**
* Public.
*/
function isMajorBrowserVersionAtLeast(minVersion) {
	return majorBrowserVersion !== null && majorBrowserVersion >= minVersion;
}
//#endregion
//#region ../../house/brave_search/shared/src/search/search-dispatcher.ts
/**
* Private.
*/
var SOURCE = "ntt";
var EVENTS = {
	QUERY_BRAVE_SEARCH_AUTOCOMPLETE: "richMediaQueryBraveSearchAutocomplete",
	OPEN_BRAVE_SEARCH_WITH_QUERY: "richMediaOpenBraveSearchWithQuery",
	HIDE_BRAVE_SEARCH_BOX: "richMediaHideBraveSearchBox",
	MAKE_BRAVE_SEARCH_DEFAULT: "richMediaMakeBraveSearchDefault"
};
function dispatchEvent_(type, value) {
	debugLog(`Dispatching ${type}${value ? ` with ${value}` : ""}`);
	postMessage({
		type,
		value,
		id: crypto.randomUUID()
	});
}
/**
* Public.
*/
function dispatchQueryAutocomplete(searchQuery) {
	dispatchEvent_(EVENTS.QUERY_BRAVE_SEARCH_AUTOCOMPLETE, searchQuery);
}
function dispatchSearchWithQuery(searchQuery) {
	const encodedQuery = encodeURIComponent(searchQuery.query);
	const extraParams = searchQuery.params ? `&${searchQuery.params}` : "";
	dispatchEvent_(EVENTS.OPEN_BRAVE_SEARCH_WITH_QUERY, `search?q=${encodedQuery}&source=${SOURCE}&action=makeDefault${extraParams}`);
}
function dispatchAskBrave(searchQuery) {
	const encodedQuery = encodeURIComponent(searchQuery);
	dispatchEvent_(EVENTS.OPEN_BRAVE_SEARCH_WITH_QUERY, `ask?q=${encodedQuery}&source=${SOURCE}`);
}
function dispatchDestinationUrl(url) {
	const { pathname, search } = new URL(url);
	const query = `${pathname}${search}`;
	dispatchEvent_(EVENTS.OPEN_BRAVE_SEARCH_WITH_QUERY, query);
}
function dispatchHideBraveSearchBox() {
	dispatchEvent_(EVENTS.HIDE_BRAVE_SEARCH_BOX, "");
}
function dispatchMakeDefault() {
	dispatchEvent_(EVENTS.MAKE_BRAVE_SEARCH_DEFAULT, "");
}
var searchDispatcher = {
	dispatchQueryAutocomplete,
	dispatchSearchWithQuery,
	dispatchAskBrave,
	dispatchDestinationUrl,
	dispatchHideBraveSearchBox,
	dispatchMakeDefault
};
//#endregion
//#region ../../house/brave_search/shared/src/search/search-mode.ts
var SearchMode = /* @__PURE__ */ function(SearchMode) {
	SearchMode["Interactive"] = "interactive";
	SearchMode["AutoTypeAssemble"] = "autotype-assemble";
	SearchMode["AutoTypeBounce"] = "autotype-bounce";
	SearchMode["AutoTypeCaret"] = "autotype-caret";
	SearchMode["AutoTypeFade"] = "autotype-fade";
	SearchMode["AutoTypeFadeChars"] = "autotype-fade-chars";
	SearchMode["AutoTypeFocus"] = "autotype-focus";
	SearchMode["AutoTypeGhost"] = "autotype-ghost";
	SearchMode["AutoTypeMagnify"] = "autotype-magnify";
	SearchMode["AutoTypeNeon"] = "autotype-neon";
	SearchMode["AutoTypeRandom"] = "autotype-random";
	SearchMode["AutoTypeRedact"] = "autotype-redact";
	SearchMode["AutoTypeReducedMotion"] = "autotype-reduced-motion";
	SearchMode["AutoTypeReveal"] = "autotype-reveal";
	SearchMode["AutoTypeScramble"] = "autotype-scramble";
	SearchMode["AutoTypeSlotMachine"] = "autotype-slot-machine";
	SearchMode["AutoTypeSprinkle"] = "autotype-sprinkle";
	SearchMode["AutoTypeWaterfall"] = "autotype-waterfall";
	SearchMode["AutoTypeWordBurst"] = "autotype-word-burst";
	return SearchMode;
}({});
//#endregion
//#region ../../apis/random.ts
/**
* Public.
*/
function intInRange(min, max, { inclusive = true } = {}) {
	if (!Number.isFinite(min) || !Number.isFinite(max)) throw new Error("min and max must be finite.");
	if (inclusive ? min > max : min >= max) throw new Error(inclusive ? "min must be less than or equal to max." : "min must be less than max.");
	const range = max - min + (inclusive ? 1 : 0);
	return Math.floor(Math.random() * range) + min;
}
function shuffle(array) {
	const shuffled = [...array];
	for (let i = shuffled.length; i > 1; i--) {
		const j = Math.floor(Math.random() * i);
		[shuffled[i - 1], shuffled[j]] = [shuffled[j], shuffled[i - 1]];
	}
	return shuffled;
}
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/caret/mouse-pointer.ts
/**
* Private.
*/
var mousePointerElement_ = null;
/**
* Public.
*/
function init$24(containerElement) {
	const mousePointerElement = document.createElement("div");
	mousePointerElement.id = "mouse-pointer";
	mousePointerElement.classList.add("hidden");
	containerElement.appendChild(mousePointerElement);
	mousePointerElement_ = mousePointerElement;
}
function show() {
	mousePointerElement_?.classList.remove("hidden");
}
function hide$1() {
	mousePointerElement_?.classList.add("hidden");
}
function moveTo(x, y, delay = 0) {
	if (!mousePointerElement_) return;
	mousePointerElement_.style.transition = delay ? `left ${delay}ms ease-in-out, top ${delay}ms ease-in-out` : "none";
	mousePointerElement_.style.left = `${x}px`;
	mousePointerElement_.style.top = `${y}px`;
}
function getElement() {
	return mousePointerElement_;
}
var mousePointer = {
	init: init$24,
	show,
	hide: hide$1,
	moveTo,
	getElement
};
//#endregion
//#region ../../house/brave_search/shared/src/gestures/simulate-tap.config.ts
var simulateTapConfig = {
	tapDuration: 1200,
	tapButtonAnimationDuration: 833,
	tapIndicatorSize: 54,
	tapIndicatorMaxScale: 2.5,
	tapIndicatorMinScale: 2.1,
	tapIndicatorColor: "rgba(255, 255, 255, 0.5)"
};
//#endregion
//#region ../../house/brave_search/shared/src/gestures/simulate-tap.ts
/**
* Private.
*/
var buttonElement_ = null;
/**
* Public.
*/
function init$23() {
	buttonElement_ = document.getElementById("try-now-button");
	if (!buttonElement_) return;
	buttonElement_.addEventListener("mouseenter", stop);
	buttonElement_.style.setProperty("--tap-duration", `${simulateTapConfig.tapDuration}ms`);
	buttonElement_.style.setProperty("--tap-indicator-size", `${simulateTapConfig.tapIndicatorSize}px`);
	buttonElement_.style.setProperty("--tap-indicator-max-scale", `${simulateTapConfig.tapIndicatorMaxScale}`);
	buttonElement_.style.setProperty("--tap-indicator-min-scale", `${simulateTapConfig.tapIndicatorMinScale}`);
	buttonElement_.style.setProperty("--tap-indicator-color", simulateTapConfig.tapIndicatorColor);
}
function start$1() {
	buttonElement_?.classList.add("animate-tap");
}
function stop() {
	buttonElement_?.classList.remove("animate-tap", "animate");
}
function startAnim() {
	buttonElement_?.classList.add("animate");
}
var simulateTap = {
	init: init$23,
	start: start$1,
	stop,
	startAnim
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/caret/config.ts
var searchQuery = {
	searchQueryTextColor: "white",
	minTypingDelayMs: 30,
	maxTypingDelayMs: 90
};
var mousePointerConfig = { mouseMoveDurationMs: 1e3 };
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/auto-type.config.ts
var autoTypeConfig = {
	placeholderFadeAfterMs: 500,
	placeholderFadeDurationMs: 200,
	placeholderFadeInDurationMs: 300,
	placeholderColor: "#a1a1aa",
	searchQueryTextColor: "white",
	simulateTryNowButtonTapAfterMs: 300,
	searchResultAppearsFactor: .25,
	nextSearchResultQueryAfterMs: 2500,
	contentSlideUpOnResultVisible: -40,
	contentSlideUpDurationMs: 900,
	contentSlideUpEasing: "cubic-bezier(0.16, 1, 0.3, 1)",
	searchResultImageMaxWidth: 900,
	searchResultImageBorderRadius: 8,
	searchResultImageTopOffset: -15,
	searchResultImageFadeInDurationMs: 400
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/auto-type-utils.ts
function createScheduler() {
	const timeoutIds = /* @__PURE__ */ new Set();
	const intervalIds = /* @__PURE__ */ new Set();
	function scheduleAfter(onComplete, delay) {
		const id = setTimeout(() => {
			timeoutIds.delete(id);
			onComplete();
		}, delay);
		timeoutIds.add(id);
	}
	function scheduleEvery(onTick, delay) {
		const id = setInterval(onTick, delay);
		intervalIds.add(id);
	}
	function cancelAll() {
		timeoutIds.forEach(clearTimeout);
		timeoutIds.clear();
		intervalIds.forEach(clearInterval);
		intervalIds.clear();
	}
	return {
		scheduleAfter,
		scheduleEvery,
		cancelAll
	};
}
function splitWords(searchQueryText) {
	return searchQueryText.split(" ");
}
function createWordSpanElements(element, searchQueryWords) {
	return searchQueryWords.map((_, wordIndex) => {
		const spanElement = document.createElement("span");
		spanElement.style.cssText = "display:inline-block;white-space:nowrap";
		element.appendChild(spanElement);
		if (wordIndex < searchQueryWords.length - 1) element.appendChild(document.createTextNode(" "));
		return spanElement;
	});
}
function splitSearchModes(raw) {
	return raw.split(",").map((searchMode) => searchMode.trim()).filter(Boolean);
}
function avoidRepeatAtStart(array, lastItem) {
	if (array[0] === lastItem && array.length > 1) {
		array.shift();
		array.push(lastItem);
	}
}
function shuffledIndices(length) {
	const indices = Array.from({ length }, (_, i) => i);
	for (let i = indices.length - 1; i > 0; i--) {
		const j = Math.floor(Math.random() * (i + 1));
		[indices[i], indices[j]] = [indices[j], indices[i]];
	}
	return indices;
}
function scheduleCharacterAnimations(searchQueryWords, searchQueryWordSpanElements, characterStaggerDelay, onCharacter, schedule) {
	let staggerIndex = 0;
	searchQueryWords.forEach((word, wordIndex) => {
		const wordSpanElement = searchQueryWordSpanElements[wordIndex];
		const characters = [...word];
		for (const character of characters) schedule(() => onCharacter(wordSpanElement, character), staggerIndex++ * characterStaggerDelay);
		if (wordIndex < searchQueryWords.length - 1) staggerIndex++;
	});
}
function fadeOutPlaceholder(searchQueryElement, schedule, onComplete) {
	schedule(() => {
		searchQueryElement.style.setProperty("--fade-out-duration", `${autoTypeConfig.placeholderFadeDurationMs}ms`);
		searchQueryElement.classList.add("search-query-fade-out");
		schedule(onComplete, autoTypeConfig.placeholderFadeDurationMs);
	}, autoTypeConfig.placeholderFadeAfterMs);
}
function scheduleAnimationComplete(animationDuration, schedule, onTapTryNowButton, onShowSearchResult, onStartNextSearchQuery) {
	const showSearchResultAfter = autoTypeConfig.simulateTryNowButtonTapAfterMs + simulateTapConfig.tapButtonAnimationDuration * autoTypeConfig.searchResultAppearsFactor;
	const startNextQueryAfter = Math.max(animationDuration, showSearchResultAfter) + autoTypeConfig.nextSearchResultQueryAfterMs;
	schedule(onTapTryNowButton, autoTypeConfig.simulateTryNowButtonTapAfterMs);
	schedule(onShowSearchResult, showSearchResultAfter);
	schedule(onStartNextSearchQuery, startNextQueryAfter);
}
function showSearchResult(imageElement, contentElement, searchResultImageSrc) {
	if (!searchResultImageSrc) {
		hideSearchResult$1(imageElement);
		return;
	}
	imageElement.src = searchResultImageSrc;
	imageElement.getBoundingClientRect();
	imageElement.classList.add("visible");
	contentElement.classList.add("search-result-visible");
}
function hideSearchResult$1(imageElement) {
	if (!imageElement) return;
	imageElement.classList.remove("visible");
}
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/caret/index.ts
/**
* Private.
*/
var searchQueryElement_$17;
var searchResultImageElement_$17;
var contentElement_$17;
var mousePointerContainerElement_ = null;
var tryNowButtonElement_ = null;
var showMousePointer_ = true;
var { scheduleAfter: scheduleAfter$16, cancelAll: cancelAll$16 } = createScheduler();
function toOverlayCoords_(targetElement) {
	const elementRect = targetElement.getBoundingClientRect();
	const overlayRect = (mousePointerContainerElement_ ?? document.body).getBoundingClientRect();
	return {
		left: elementRect.left - overlayRect.left,
		top: elementRect.top - overlayRect.top,
		width: elementRect.width,
		height: elementRect.height
	};
}
function calculateTryNowButtonPosition_() {
	if (!tryNowButtonElement_) return {
		x: 0,
		y: 0
	};
	const { left, top, width, height } = toOverlayCoords_(tryNowButtonElement_);
	return {
		x: left + width / 2,
		y: top + height / 2
	};
}
function calculateSearchBoxPosition_() {
	const { left, top, height } = toOverlayCoords_(searchQueryElement_$17);
	return {
		x: left,
		y: top + height / 2
	};
}
function moveMousePointerTo_(x, y, onComplete, duration = 0) {
	mousePointer.moveTo(x, y, duration);
	if (onComplete) scheduleAfter$16(onComplete, duration);
}
function showCursorAtStart_() {
	searchQueryElement_$17.classList.remove("search-query-cursor", "search-query-cursor-space");
	searchQueryElement_$17.classList.add("search-query-cursor-left");
}
function showCursor_() {
	searchQueryElement_$17.classList.remove("search-query-cursor-left", "search-query-cursor-space");
	searchQueryElement_$17.classList.add("search-query-cursor");
}
function hideCursor_() {
	searchQueryElement_$17.classList.remove("search-query-cursor", "search-query-cursor-left", "search-query-cursor-space");
}
function showCaretSpace_() {
	searchQueryElement_$17.classList.remove("search-query-cursor", "search-query-cursor-left");
	searchQueryElement_$17.classList.add("search-query-cursor-space");
}
function resetSearchQuery_$16() {
	if (!searchQueryElement_$17) return;
	showCaretSpace_();
	searchQueryElement_$17.classList.remove("search-query-fade-in", "search-query-fade-out");
}
function initTyping_() {
	searchQueryElement_$17.textContent = "";
	searchQueryElement_$17.style.color = searchQuery.searchQueryTextColor;
	showCursor_();
}
function startAnimatingButton_() {
	tryNowButtonElement_?.classList.add("animate");
}
function stopAnimatingButton_() {
	tryNowButtonElement_?.classList.remove("animate");
}
function simulateTyping_(searchQueryText, onComplete, onTypingStart) {
	fadeOutPlaceholder(searchQueryElement_$17, scheduleAfter$16, startTyping);
	function startTyping() {
		searchQueryElement_$17.classList.remove("search-query-fade-in", "search-query-fade-out");
		initTyping_();
		if (onTypingStart) onTypingStart();
		let typingCharacterIndex = 0;
		function typeCharacter() {
			if (typingCharacterIndex > searchQueryText.length) {
				if (onComplete) onComplete();
				return;
			}
			searchQueryElement_$17.textContent = searchQueryText.slice(0, typingCharacterIndex++);
			scheduleAfter$16(typeCharacter, intInRange(searchQuery.minTypingDelayMs, searchQuery.maxTypingDelayMs));
		}
		typeCharacter();
	}
}
function simulateTypingAndMouse_(searchQueryText, onComplete, onTypingStart) {
	simulateTyping_(searchQueryText, () => {
		hideCursor_();
		if (showMousePointer_) {
			const caretPosition = calculateSearchBoxPosition_();
			mousePointer.moveTo(caretPosition.x, caretPosition.y);
			mousePointer.show();
		}
		onComplete();
	}, onTypingStart);
}
function simulateWithTap_(searchQueryText, searchResultImageSrc, onComplete) {
	const onTypingStart = () => {
		scheduleAfter$16(() => {
			simulateTap.start();
			simulateTap.startAnim();
		}, autoTypeConfig.simulateTryNowButtonTapAfterMs);
		scheduleAfter$16(() => showSearchResult(searchResultImageElement_$17, contentElement_$17, searchResultImageSrc), autoTypeConfig.simulateTryNowButtonTapAfterMs + simulateTapConfig.tapButtonAnimationDuration * autoTypeConfig.searchResultAppearsFactor);
	};
	const onTypingComplete = () => {
		scheduleAfter$16(() => {
			simulateTap.stop();
			searchResultImageElement_$17.classList.remove("visible");
			onComplete();
		}, autoTypeConfig.nextSearchResultQueryAfterMs);
	};
	simulateTypingAndMouse_(searchQueryText, onTypingComplete, onTypingStart);
}
function simulateWithMousePointer_(searchQueryText, searchResultImageSrc, onComplete) {
	const onTypingComplete = () => {
		const buttonPosition = calculateTryNowButtonPosition_();
		moveMousePointerTo_(buttonPosition.x, buttonPosition.y, onMouseMovedToButton, mousePointerConfig.mouseMoveDurationMs);
	};
	function onMouseMovedToButton() {
		mousePointer.hide();
		startAnimatingButton_();
		scheduleAnimationComplete(autoTypeConfig.simulateTryNowButtonTapAfterMs, scheduleAfter$16, () => {}, () => showSearchResult(searchResultImageElement_$17, contentElement_$17, searchResultImageSrc), () => {});
		scheduleAfter$16(() => {
			stopAnimatingButton_();
			onButtonAnimationComplete();
		}, autoTypeConfig.nextSearchResultQueryAfterMs);
	}
	function onButtonAnimationComplete() {
		const caretPosition = calculateSearchBoxPosition_();
		searchResultImageElement_$17.classList.remove("visible");
		const buttonPosition = calculateTryNowButtonPosition_();
		mousePointer.moveTo(buttonPosition.x, buttonPosition.y);
		mousePointer.show();
		mousePointer.getElement()?.offsetLeft;
		moveMousePointerTo_(caretPosition.x, caretPosition.y, () => {
			mousePointer.hide();
			onComplete();
		}, mousePointerConfig.mouseMoveDurationMs);
	}
	simulateTypingAndMouse_(searchQueryText, onTypingComplete);
}
function stopAnimation_$17() {
	mousePointer.hide();
	cancelAll$16();
	resetSearchQuery_$16();
}
/**
* Public.
*/
function init$22({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$17();
	searchQueryElement_$17 = searchQueryElement;
	searchResultImageElement_$17 = searchResultImageElement;
	contentElement_$17 = contentElement;
	mousePointerContainerElement_ = contentElement.parentElement ?? document.body;
	tryNowButtonElement_ = document.getElementById("try-now-button");
	showMousePointer_ = searchQueryElement.closest(".search-box")?.dataset.hideMousePointer === void 0;
	if (showMousePointer_) mousePointer.init(mousePointerContainerElement_);
	else simulateTap.init();
}
function prepare$1() {
	showCursorAtStart_();
	const caretPosition = calculateSearchBoxPosition_();
	mousePointer.moveTo(caretPosition.x, caretPosition.y);
}
function simulate$17(searchQueryText, searchResultImageSrc, onComplete) {
	if (showMousePointer_) simulateWithMousePointer_(searchQueryText, searchResultImageSrc, onComplete);
	else simulateWithTap_(searchQueryText, searchResultImageSrc, onComplete);
}
function cancel$17() {
	stopAnimation_$17();
}
var autoTypeCaret = {
	init: init$22,
	prepare: prepare$1,
	simulate: simulate$17,
	cancel: cancel$17
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/fade-chars/config.ts
var CONFIG$15 = {
	minTypingDelayMs: 10,
	maxTypingDelayMs: 20
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/fade-chars/index.ts
/**
* Private.
*/
var searchQueryElement_$16;
var searchResultImageElement_$16;
var contentElement_$16;
var { scheduleAfter: scheduleAfter$15, cancelAll: cancelAll$15 } = createScheduler();
function resetSearchQuery_$15() {
	if (!searchQueryElement_$16) return;
	searchQueryElement_$16.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$16.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$16.textContent = "";
}
function simulateTypingCharacter_(searchQueryText, characterIndex) {
	if (characterIndex >= searchQueryText.length) return;
	const spanElement = document.createElement("span");
	spanElement.textContent = searchQueryText[characterIndex];
	spanElement.classList.add("search-query-character-fade-in");
	searchQueryElement_$16.appendChild(spanElement);
	scheduleAfter$15(() => simulateTypingCharacter_(searchQueryText, characterIndex + 1), intInRange(CONFIG$15.minTypingDelayMs, CONFIG$15.maxTypingDelayMs));
}
function stopAnimation_$16() {
	cancelAll$15();
	resetSearchQuery_$15();
}
function onSearchQueryComplete_$13(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$16);
	resetSearchQuery_$15();
	onComplete();
}
function animateSearchQuery_$15(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$15();
	scheduleAnimationComplete(searchQueryText.length * ((CONFIG$15.minTypingDelayMs + CONFIG$15.maxTypingDelayMs) / 2), scheduleAfter$15, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$16, contentElement_$16, searchResultImageSrc), () => onSearchQueryComplete_$13(onComplete));
	simulateTypingCharacter_(searchQueryText, 0);
}
/**
* Public.
*/
function init$21({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$16();
	searchQueryElement_$16 = searchQueryElement;
	searchResultImageElement_$16 = searchResultImageElement;
	contentElement_$16 = contentElement;
	simulateTap.init();
}
function simulate$16(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$16, scheduleAfter$15, () => animateSearchQuery_$15(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$16() {
	stopAnimation_$16();
}
var autoTypeFadeChars = {
	init: init$21,
	simulate: simulate$16,
	cancel: cancel$16
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/fade/config.ts
var CONFIG$14 = {
	searchQueryFadeInDurationMs: 1800,
	searchQueryFadeOutDurationMs: 400
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/fade/index.ts
/**
* Private.
*/
var searchQueryElement_$15;
var searchResultImageElement_$15;
var contentElement_$15;
var { scheduleAfter: scheduleAfter$14, cancelAll: cancelAll$14 } = createScheduler();
function resetSearchQuery_$14() {
	if (!searchQueryElement_$15) return;
	searchQueryElement_$15.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$15.textContent = "";
}
function stopAnimation_$15() {
	cancelAll$14();
	resetSearchQuery_$14();
}
function fadeOutSearchQuery_(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$15);
	searchQueryElement_$15.style.setProperty("--fade-out-duration", `${CONFIG$14.searchQueryFadeOutDurationMs}ms`);
	searchQueryElement_$15.classList.add("search-query-fade-out");
	scheduleAfter$14(() => {
		searchQueryElement_$15.classList.remove("search-query-fade-out");
		onComplete();
	}, CONFIG$14.searchQueryFadeOutDurationMs);
}
function animateSearchQuery_$14(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	searchQueryElement_$15.style.opacity = "0";
	searchQueryElement_$15.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$15.textContent = searchQueryText;
	searchQueryElement_$15.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$15.offsetWidth;
	searchQueryElement_$15.style.opacity = "";
	searchQueryElement_$15.style.setProperty("--fade-in-duration", `${CONFIG$14.searchQueryFadeInDurationMs}ms`);
	searchQueryElement_$15.classList.add("search-query-fade-in");
	scheduleAnimationComplete(CONFIG$14.searchQueryFadeInDurationMs, scheduleAfter$14, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$15, contentElement_$15, searchResultImageSrc), () => fadeOutSearchQuery_(onComplete));
}
/**
* Public.
*/
function init$20({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$15();
	searchQueryElement_$15 = searchQueryElement;
	searchResultImageElement_$15 = searchResultImageElement;
	contentElement_$15 = contentElement;
	simulateTap.init();
}
function simulate$15(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$15, scheduleAfter$14, () => animateSearchQuery_$14(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$15() {
	stopAnimation_$15();
}
var autoTypeFade = {
	init: init$20,
	simulate: simulate$15,
	cancel: cancel$15
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/scramble/config.ts
var CONFIG$13 = {
	characterStaggerDelayMs: 30,
	randomCharRefreshIntervalMs: 45,
	randomCharDurationMs: 180
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/scramble/index.ts
/**
* Private.
*/
var CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
var searchQueryElement_$14;
var searchResultImageElement_$14;
var contentElement_$14;
var cancelled_ = false;
var { scheduleAfter: scheduleAfter$13, scheduleEvery, cancelAll: cancelAll$13 } = createScheduler();
function stopAnimation_$14() {
	cancelled_ = true;
	cancelAll$13();
	resetSearchQuery_$13();
}
function resetSearchQuery_$13() {
	if (!searchQueryElement_$14) return;
	searchQueryElement_$14.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$14.style.height = "";
	searchQueryElement_$14.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$14.textContent = "";
}
function randomChar_(previousChar) {
	let character;
	do
		character = CHARSET[Math.floor(Math.random() * 62)];
	while (character === previousChar);
	return character;
}
function scheduleCharacterScramble_(spanElement, targetCharacter, staggerDelay) {
	scheduleAfter$13(() => {
		if (cancelled_) return;
		spanElement.textContent = randomChar_("");
		spanElement.style.opacity = "1";
		const scrambleStart = Date.now();
		let scrambleDone = false;
		scheduleEvery(() => {
			if (scrambleDone || cancelled_) {
				scrambleDone = true;
				return;
			}
			if (Date.now() >= scrambleStart + CONFIG$13.randomCharDurationMs) {
				scrambleDone = true;
				spanElement.textContent = targetCharacter;
				spanElement.classList.add("search-query-character-scramble-locked");
			} else spanElement.textContent = randomChar_(spanElement.textContent ?? "");
		}, CONFIG$13.randomCharRefreshIntervalMs);
	}, staggerDelay);
}
function scheduleCharacterScrambles_(searchQueryText) {
	const characters = [...searchQueryText];
	if (characters.filter((character) => character !== " ").length === 0) return;
	const spanEntries = characters.map((character) => {
		if (character === " ") {
			searchQueryElement_$14.appendChild(document.createTextNode(" "));
			return null;
		}
		const spanElement = document.createElement("span");
		spanElement.textContent = character;
		spanElement.style.opacity = "0";
		spanElement.classList.add("search-query-character-scramble");
		searchQueryElement_$14.appendChild(spanElement);
		return {
			spanElement,
			targetCharacter: character
		};
	});
	characters.forEach((character, characterIndex) => {
		if (character === " ") return;
		const entry = spanEntries[characterIndex];
		if (!entry) return;
		scheduleCharacterScramble_(entry.spanElement, entry.targetCharacter, characterIndex * CONFIG$13.characterStaggerDelayMs);
	});
}
function onSearchQueryComplete_$12(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$14);
	resetSearchQuery_$13();
	onComplete();
}
function animateSearchQuery_$13(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	searchQueryElement_$14.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$14.textContent = "";
	searchQueryElement_$14.style.color = autoTypeConfig.searchQueryTextColor;
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG$13.characterStaggerDelayMs + CONFIG$13.randomCharDurationMs, scheduleAfter$13, () => simulateTap.startAnim(), () => {
		if (!cancelled_) showSearchResult(searchResultImageElement_$14, contentElement_$14, searchResultImageSrc);
	}, () => onSearchQueryComplete_$12(onComplete));
	scheduleCharacterScrambles_(searchQueryText);
	searchQueryElement_$14.style.height = `${searchQueryElement_$14.offsetHeight}px`;
}
/**
* Public.
*/
function init$19({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$14();
	cancelled_ = false;
	searchQueryElement_$14 = searchQueryElement;
	searchResultImageElement_$14 = searchResultImageElement;
	contentElement_$14 = contentElement;
	simulateTap.init();
}
function simulate$14(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$14, scheduleAfter$13, () => animateSearchQuery_$13(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$14() {
	stopAnimation_$14();
}
var autoTypeScramble = {
	init: init$19,
	simulate: simulate$14,
	cancel: cancel$14
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/word-burst/config.ts
var CONFIG$12 = {
	delayBetweenWordsMs: 150,
	wordBurstDurationMs: 400,
	wordBurstEasing: "cubic-bezier(0.34, 1.56, 0.64, 1)"
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/word-burst/index.ts
/**
* Private.
*/
var BURST_VARIANTS = [
	"word-burst-scale",
	"word-burst-squish",
	"word-burst-spin",
	"word-burst-punch"
];
var searchQueryElement_$13;
var searchResultImageElement_$13;
var contentElement_$13;
var { scheduleAfter: scheduleAfter$12, cancelAll: cancelAll$12 } = createScheduler();
function resetSearchQuery_$12() {
	if (!searchQueryElement_$13) return;
	searchQueryElement_$13.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$13.style.overflow = "";
	searchQueryElement_$13.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$13.textContent = "";
}
function applyBurstAnimation_(spanElement, lastVariant, spinDirectionAngle) {
	let variant;
	do
		variant = BURST_VARIANTS[Math.floor(Math.random() * BURST_VARIANTS.length)];
	while (variant === lastVariant);
	const burstScale = (1.1 + Math.random() * .25).toFixed(2);
	const burstRotate = (Math.random() * 20 - 10).toFixed(1);
	const spinStart = spinDirectionAngle === "180deg" ? "-180deg" : "180deg";
	spanElement.style.setProperty("--word-burst-scale", burstScale);
	spanElement.style.setProperty("--word-burst-rotate", `${burstRotate}deg`);
	spanElement.style.setProperty("--word-burst-spin-start", spinStart);
	spanElement.style.animation = `${variant} ${CONFIG$12.wordBurstDurationMs}ms ${CONFIG$12.wordBurstEasing} forwards`;
	spanElement.classList.add("search-query-word-burst");
	return {
		nextVariant: variant,
		nextSpinDirectionAngle: variant === "word-burst-spin" ? spinStart : spinDirectionAngle
	};
}
function scheduleWordAnimations_(words) {
	let lastVariant = null;
	let spinDirectionAngle = "180deg";
	words.forEach((word, wordIndex) => {
		scheduleAfter$12(() => {
			const spanElement = document.createElement("span");
			spanElement.textContent = word;
			const burst = applyBurstAnimation_(spanElement, lastVariant, spinDirectionAngle);
			lastVariant = burst.nextVariant;
			spinDirectionAngle = burst.nextSpinDirectionAngle;
			searchQueryElement_$13.appendChild(spanElement);
			if (wordIndex < words.length - 1) searchQueryElement_$13.appendChild(document.createTextNode(" "));
		}, wordIndex * CONFIG$12.delayBetweenWordsMs);
	});
}
function stopAnimation_$13() {
	cancelAll$12();
	resetSearchQuery_$12();
}
function onSearchQueryComplete_$11(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$13);
	resetSearchQuery_$12();
	onComplete();
}
function unclipSearchQuery_$5() {
	searchQueryElement_$13.style.overflow = "visible";
}
function animateSearchQuery_$12(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$12();
	unclipSearchQuery_$5();
	const words = splitWords(searchQueryText);
	scheduleWordAnimations_(words);
	scheduleAnimationComplete((words.length - 1) * CONFIG$12.delayBetweenWordsMs + CONFIG$12.wordBurstDurationMs, scheduleAfter$12, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$13, contentElement_$13, searchResultImageSrc), () => onSearchQueryComplete_$11(onComplete));
}
/**
* Public.
*/
function init$18({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$13();
	searchQueryElement_$13 = searchQueryElement;
	searchResultImageElement_$13 = searchResultImageElement;
	contentElement_$13 = contentElement;
	simulateTap.init();
}
function simulate$13(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$13, scheduleAfter$12, () => animateSearchQuery_$12(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$13() {
	stopAnimation_$13();
}
var autoTypeWordBurst = {
	init: init$18,
	simulate: simulate$13,
	cancel: cancel$13
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/bounce/config.ts
var CONFIG$11 = {
	characterStaggerDelayMs: 25,
	bounceDurationMs: 360,
	bounceDurationJitterMinMs: 80,
	bounceDurationJitterMaxMs: 120,
	characterSettleDurationMs: 120,
	fallHeightMin: -28,
	fallHeightMax: -6,
	springOvershootMin: 2,
	springOvershootMax: 10,
	characterBounceInEasing: "cubic-bezier(0.22, 1, 0.36, 1)"
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/bounce/index.ts
/**
* Private.
*/
var searchQueryElement_$12;
var searchResultImageElement_$12;
var contentElement_$12;
var { scheduleAfter: scheduleAfter$11, cancelAll: cancelAll$11 } = createScheduler();
function resetSearchQuery_$11() {
	if (!searchQueryElement_$12) return;
	searchQueryElement_$12.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$12.style.overflow = "";
	searchQueryElement_$12.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$12.textContent = "";
}
function unclipSearchQuery_$4() {
	searchQueryElement_$12.style.overflow = "visible";
}
function createCharacterSpanElement_$3(character) {
	const spanElement = document.createElement("span");
	spanElement.textContent = character;
	const fromY = intInRange(CONFIG$11.fallHeightMin, CONFIG$11.fallHeightMax);
	const springOvershoot = intInRange(CONFIG$11.springOvershootMin, CONFIG$11.springOvershootMax);
	const duration = intInRange(CONFIG$11.bounceDurationMs - CONFIG$11.bounceDurationJitterMinMs, CONFIG$11.bounceDurationMs + CONFIG$11.bounceDurationJitterMaxMs);
	spanElement.style.setProperty("--bounce-from-y", `${fromY}px`);
	spanElement.style.setProperty("--bounce-overshoot", `${springOvershoot}px`);
	spanElement.style.setProperty("--bounce-in-duration", `${duration}ms`);
	spanElement.classList.add("search-query-character-bounce");
	return spanElement;
}
function appendCharacterSpan_$4(wordSpanElement, character) {
	wordSpanElement.appendChild(createCharacterSpanElement_$3(character));
}
function stopAnimation_$12() {
	cancelAll$11();
	resetSearchQuery_$11();
}
function onSearchQueryComplete_$10(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$12);
	resetSearchQuery_$11();
	onComplete();
}
function animateSearchQuery_$11(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$11();
	unclipSearchQuery_$4();
	const words = splitWords(searchQueryText);
	scheduleCharacterAnimations(words, createWordSpanElements(searchQueryElement_$12, words), CONFIG$11.characterStaggerDelayMs, appendCharacterSpan_$4, scheduleAfter$11);
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG$11.characterStaggerDelayMs + CONFIG$11.bounceDurationMs + CONFIG$11.characterSettleDurationMs, scheduleAfter$11, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$12, contentElement_$12, searchResultImageSrc), () => onSearchQueryComplete_$10(onComplete));
}
/**
* Public.
*/
function init$17({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$12();
	searchQueryElement_$12 = searchQueryElement;
	searchQueryElement_$12.style.setProperty("--bounce-in-easing", CONFIG$11.characterBounceInEasing);
	searchResultImageElement_$12 = searchResultImageElement;
	contentElement_$12 = contentElement;
	simulateTap.init();
}
function simulate$12(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$12, scheduleAfter$11, () => animateSearchQuery_$11(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$12() {
	stopAnimation_$12();
}
var autoTypeBounce = {
	init: init$17,
	simulate: simulate$12,
	cancel: cancel$12
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/reveal/config.ts
var CONFIG$10 = {
	revealDurationMs: 600,
	revealEasing: "cubic-bezier(0.4, 0, 0.2, 1)"
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/reveal/index.ts
/**
* Private.
*/
var searchQueryElement_$11;
var searchResultImageElement_$11;
var contentElement_$11;
var { scheduleAfter: scheduleAfter$10, cancelAll: cancelAll$10 } = createScheduler();
function resetSearchQuery_$10() {
	if (!searchQueryElement_$11) return;
	searchQueryElement_$11.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$11.classList.remove("search-query-fade-in", "search-query-fade-out", "search-query-reveal-ltr");
	searchQueryElement_$11.textContent = "";
}
function stopAnimation_$11() {
	cancelAll$10();
	resetSearchQuery_$10();
}
function onSearchQueryComplete_$9(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$11);
	resetSearchQuery_$10();
	onComplete();
}
function startRevealAnimation_(searchQueryText) {
	searchQueryElement_$11.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$11.textContent = searchQueryText;
	searchQueryElement_$11.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$11.offsetWidth;
	searchQueryElement_$11.classList.add("search-query-reveal-ltr");
}
function animateSearchQuery_$10(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	startRevealAnimation_(searchQueryText);
	scheduleAnimationComplete(CONFIG$10.revealDurationMs, scheduleAfter$10, () => simulateTap.startAnim(), () => {
		searchQueryElement_$11.classList.remove("search-query-reveal-ltr");
		showSearchResult(searchResultImageElement_$11, contentElement_$11, searchResultImageSrc);
	}, () => onSearchQueryComplete_$9(onComplete));
}
/**
* Public.
*/
function init$16({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$11();
	searchQueryElement_$11 = searchQueryElement;
	searchQueryElement_$11.style.setProperty("--reveal-ltr-duration", `${CONFIG$10.revealDurationMs}ms`);
	searchQueryElement_$11.style.setProperty("--reveal-ltr-easing", CONFIG$10.revealEasing);
	searchResultImageElement_$11 = searchResultImageElement;
	contentElement_$11 = contentElement;
	simulateTap.init();
}
function simulate$11(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$11, scheduleAfter$10, () => animateSearchQuery_$10(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$11() {
	stopAnimation_$11();
}
var autoTypeReveal = {
	init: init$16,
	simulate: simulate$11,
	cancel: cancel$11
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/slot-machine/config.ts
var CONFIG$9 = {
	characterStaggerDelayMs: 30,
	reelSpinDurationMinMs: 500,
	reelSpinDurationMaxMs: 900,
	coastSymbolCountMin: 6,
	coastSymbolCountMax: 16,
	symbolsAbovePayline: 1,
	symbolsBelowPayline: 2,
	reelSnapOvershootMin: 6,
	reelSnapOvershootMax: 16,
	winningLineAnimationDurationMs: 320,
	winningLineAnimationStaggerMs: 25,
	winningLineColor: "#ffd700",
	rightFadeMinQueryLength: 39
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/slot-machine/index.ts
/**
* Private.
*/
var searchQueryElement_$10;
var searchResultImageElement_$10;
var contentElement_$10;
var { scheduleAfter: scheduleAfter$9, cancelAll: cancelAll$9 } = createScheduler();
function shuffle_(characters) {
	const shuffled = [...characters];
	for (let i = shuffled.length - 1; i > 0; i--) {
		const j = Math.floor(Math.random() * (i + 1));
		[shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
	}
	return shuffled;
}
function createValidRow_(characters) {
	const nonSpaces = shuffle_(characters.filter((character) => character !== " "));
	const spaceCount = characters.length - nonSpaces.length;
	if (spaceCount === 0) return nonSpaces;
	const gapCount = nonSpaces.length - 1;
	const gapIndices = Array.from({ length: gapCount }, (_, i) => i);
	for (let i = gapIndices.length - 1; i > 0; i--) {
		const j = Math.floor(Math.random() * (i + 1));
		[gapIndices[i], gapIndices[j]] = [gapIndices[j], gapIndices[i]];
	}
	const spacedGaps = new Set(gapIndices.slice(0, spaceCount));
	const row = [];
	for (let i = 0; i < nonSpaces.length; i++) {
		row.push(nonSpaces[i]);
		if (spacedGaps.has(i)) row.push(" ");
	}
	const overflow = spaceCount - Math.min(spaceCount, gapCount);
	if (overflow > 0) row.splice(Math.floor(row.length / 2), 0, ...Array(overflow).fill(" "));
	return row;
}
function improveUniqueness_(row, previousRow) {
	for (let i = 0; i < row.length; i++) {
		if (row[i] === " " || row[i] !== previousRow[i]) continue;
		for (let j = i + 1; j < row.length; j++) if (row[j] !== " " && row[j] !== previousRow[i] && row[i] !== previousRow[j]) {
			[row[i], row[j]] = [row[j], row[i]];
			break;
		}
	}
}
function shuffleRow_(characters, previousRow) {
	const row = createValidRow_(characters);
	if (previousRow) improveUniqueness_(row, previousRow);
	return row;
}
function resetReelWindow_() {
	searchQueryElement_$10.style.overflow = "";
	searchQueryElement_$10.style.whiteSpace = "";
	searchQueryElement_$10.style.clipPath = "";
	searchQueryElement_$10.style.fontFamily = "";
	searchQueryElement_$10.closest(".search-box")?.classList.remove("slot-machine-active", "slot-machine-overflow");
}
function resetSearchQuery_$9() {
	if (!searchQueryElement_$10) return;
	resetReelWindow_();
	searchQueryElement_$10.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$10.textContent = "";
}
function stopAnimation_$10() {
	cancelAll$9();
	resetSearchQuery_$9();
}
function onSearchQueryComplete_$8(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$10);
	resetSearchQuery_$9();
	onComplete();
}
function setupReelWindow_() {
	searchQueryElement_$10.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$10.textContent = "";
	searchQueryElement_$10.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$10.style.overflow = "visible";
	searchQueryElement_$10.style.whiteSpace = "nowrap";
	searchQueryElement_$10.style.clipPath = "inset(-9999px 0px -9999px 0)";
	searchQueryElement_$10.style.fontFamily = "monospace";
	searchQueryElement_$10.closest(".search-box")?.classList.add("slot-machine-active");
}
function measureReelDimensions_() {
	const spanElement = document.createElement("span");
	spanElement.style.cssText = "visibility:hidden;position:absolute;";
	spanElement.textContent = "A";
	searchQueryElement_$10.appendChild(spanElement);
	const characterWidth = spanElement.offsetWidth;
	const characterHeight = spanElement.offsetHeight;
	searchQueryElement_$10.removeChild(spanElement);
	const searchBoxElement = searchQueryElement_$10.closest(".search-box");
	const barHeight = searchBoxElement ? searchBoxElement.offsetHeight : characterHeight * 3;
	return {
		characterWidth,
		characterHeight,
		barHeight,
		characterPaddingV: Math.round((barHeight - characterHeight) / 2)
	};
}
function generateShuffledRows_(searchQueryText) {
	const queryCharacters = searchQueryText.split("");
	const totalRows = CONFIG$9.symbolsAbovePayline + CONFIG$9.symbolsBelowPayline + CONFIG$9.coastSymbolCountMax;
	const shuffledRows = [];
	for (let i = 0; i < totalRows; i++) shuffledRows.push(shuffleRow_(queryCharacters, shuffledRows[i - 1]));
	return shuffledRows;
}
function createColumnReels_(searchQueryText, shuffledRows) {
	return searchQueryText.split("").map((character, characterIndex) => {
		const coastSymbolCount = intInRange(CONFIG$9.coastSymbolCountMin, CONFIG$9.coastSymbolCountMax);
		return {
			character,
			symbolsAbovePayline: shuffledRows.slice(0, CONFIG$9.symbolsAbovePayline).map((row) => row[characterIndex]),
			symbolsBelowPayline: shuffledRows.slice(CONFIG$9.symbolsAbovePayline, CONFIG$9.symbolsAbovePayline + CONFIG$9.symbolsBelowPayline).map((row) => row[characterIndex]),
			coastSymbols: shuffledRows.slice(CONFIG$9.symbolsAbovePayline + CONFIG$9.symbolsBelowPayline, CONFIG$9.symbolsAbovePayline + CONFIG$9.symbolsBelowPayline + coastSymbolCount).map((row) => row[characterIndex])
		};
	});
}
function createReelRowSpanElement_(symbol, characterHeight) {
	const spanElement = document.createElement("span");
	spanElement.textContent = symbol === " " ? "\xA0" : symbol;
	spanElement.style.height = `${characterHeight}px`;
	spanElement.style.lineHeight = `${characterHeight}px`;
	return spanElement;
}
function createReelSpanElement_(characterWidth, characterHeight) {
	const spanElement = document.createElement("span");
	spanElement.classList.add("slot-machine-reel");
	spanElement.style.width = `${characterWidth}px`;
	spanElement.style.height = `${characterHeight}px`;
	return spanElement;
}
function createReelWindowSpanElement_(characterPaddingV, barHeight) {
	const spanElement = document.createElement("span");
	spanElement.classList.add("slot-machine-reel-window");
	spanElement.style.top = `-${characterPaddingV}px`;
	spanElement.style.height = `${barHeight}px`;
	spanElement.style.setProperty("--slot-machine-opaque-start", `${characterPaddingV}px`);
	spanElement.style.setProperty("--slot-machine-opaque-end", `${barHeight - characterPaddingV}px`);
	return spanElement;
}
function applyReelStripTimingVariance_(spanElement, coastSymbolsLength, characterHeight, reelPaylineOffset) {
	const reelStripStartY = -(CONFIG$9.symbolsAbovePayline + CONFIG$9.symbolsBelowPayline + coastSymbolsLength) * characterHeight;
	spanElement.style.setProperty("--slot-machine-strip-start", `${reelStripStartY}px`);
	spanElement.style.setProperty("--slot-machine-strip-travel", `${reelPaylineOffset}px`);
	const reelSpinDuration = intInRange(CONFIG$9.reelSpinDurationMinMs, CONFIG$9.reelSpinDurationMaxMs);
	spanElement.style.setProperty("--slot-machine-reel-spin-duration", `${reelSpinDuration}ms`);
	const reelSnapOvershoot = intInRange(CONFIG$9.reelSnapOvershootMin, CONFIG$9.reelSnapOvershootMax);
	spanElement.style.setProperty("--slot-machine-reel-stop-overshoot", `${reelSnapOvershoot}px`);
}
function createReelStripSpanElement_(reel, characterHeight, reelPaylineOffset) {
	const { character, symbolsAbovePayline, symbolsBelowPayline, coastSymbols } = reel;
	const spanElement = document.createElement("span");
	spanElement.classList.add("slot-machine-reel-strip");
	for (const symbol of symbolsAbovePayline) spanElement.appendChild(createReelRowSpanElement_(symbol, characterHeight));
	const paylineSpanElement = createReelRowSpanElement_(character, characterHeight);
	paylineSpanElement.classList.add("slot-machine-payline");
	spanElement.appendChild(paylineSpanElement);
	for (const symbol of symbolsBelowPayline) spanElement.appendChild(createReelRowSpanElement_(symbol, characterHeight));
	for (const symbol of coastSymbols) spanElement.appendChild(createReelRowSpanElement_(symbol, characterHeight));
	applyReelStripTimingVariance_(spanElement, coastSymbols.length, characterHeight, reelPaylineOffset);
	return spanElement;
}
function scheduleReelSpins_(columnReels, dimensions, onReelsSettled) {
	const { characterWidth, characterHeight, barHeight, characterPaddingV } = dimensions;
	const reelPaylineOffset = characterPaddingV - CONFIG$9.symbolsAbovePayline * characterHeight;
	columnReels.forEach((reel, columnIndex) => {
		scheduleAfter$9(() => {
			const reelStripSpanElement = createReelStripSpanElement_(reel, characterHeight, reelPaylineOffset);
			const reelWindowSpanElement = createReelWindowSpanElement_(characterPaddingV, barHeight);
			const reelSpanElement = createReelSpanElement_(characterWidth, characterHeight);
			reelWindowSpanElement.appendChild(reelStripSpanElement);
			reelSpanElement.appendChild(reelWindowSpanElement);
			searchQueryElement_$10.appendChild(reelSpanElement);
		}, columnIndex * CONFIG$9.characterStaggerDelayMs);
	});
	scheduleAfter$9(onReelsSettled, (columnReels.length - 1) * CONFIG$9.characterStaggerDelayMs + CONFIG$9.reelSpinDurationMaxMs);
}
function scheduleWinningLineAnimation_() {
	Array.from(searchQueryElement_$10.querySelectorAll(".slot-machine-reel")).forEach((reelSpanElement, reelIndex) => {
		scheduleAfter$9(() => {
			reelSpanElement.style.setProperty("--slot-machine-winning-line-duration", `${CONFIG$9.winningLineAnimationDurationMs}ms`);
			reelSpanElement.style.setProperty("--slot-machine-winning-line-color", CONFIG$9.winningLineColor);
			reelSpanElement.classList.add("slot-machine-winning-line");
		}, reelIndex * CONFIG$9.winningLineAnimationStaggerMs);
	});
}
function animateSearchQuery_$9(searchQueryText, searchResultImageSrc, onComplete, isTruncated) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	setupReelWindow_();
	if (isTruncated) searchQueryElement_$10.closest(".search-box")?.classList.add("slot-machine-overflow");
	const reelDimensions = measureReelDimensions_();
	scheduleReelSpins_(createColumnReels_(searchQueryText, generateShuffledRows_(searchQueryText)), reelDimensions, scheduleWinningLineAnimation_);
	const lastReelIndex = searchQueryText.length - 1;
	scheduleAnimationComplete(lastReelIndex * CONFIG$9.characterStaggerDelayMs + CONFIG$9.reelSpinDurationMaxMs + lastReelIndex * CONFIG$9.winningLineAnimationStaggerMs + CONFIG$9.winningLineAnimationDurationMs, scheduleAfter$9, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$10, contentElement_$10, searchResultImageSrc), () => onSearchQueryComplete_$8(onComplete));
}
/**
* Public.
*/
function init$15({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$10();
	searchQueryElement_$10 = searchQueryElement;
	searchResultImageElement_$10 = searchResultImageElement;
	contentElement_$10 = contentElement;
	simulateTap.init();
}
function simulate$10(searchQueryText, searchResultImageSrc, onComplete) {
	const isTruncated = searchQueryText.length > CONFIG$9.rightFadeMinQueryLength;
	const displayText = searchQueryText.slice(0, CONFIG$9.rightFadeMinQueryLength);
	fadeOutPlaceholder(searchQueryElement_$10, scheduleAfter$9, () => animateSearchQuery_$9(displayText, searchResultImageSrc, onComplete, isTruncated));
}
function cancel$10() {
	stopAnimation_$10();
}
var autoTypeSlotMachine = {
	init: init$15,
	simulate: simulate$10,
	cancel: cancel$10
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/focus/config.ts
var CONFIG$8 = {
	characterStaggerDelayMs: 20,
	focusPullDurationMinMs: 1200,
	focusPullDurationMaxMs: 2600,
	defocusAmountMin: 12,
	defocusAmountMax: 32
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/focus/index.ts
/**
* Private.
*/
var searchQueryElement_$9;
var searchResultImageElement_$9;
var contentElement_$9;
var { scheduleAfter: scheduleAfter$8, cancelAll: cancelAll$8 } = createScheduler();
function resetSearchQuery_$8() {
	if (!searchQueryElement_$9) return;
	searchQueryElement_$9.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$9.style.overflow = "";
	searchQueryElement_$9.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$9.textContent = "";
}
function unclipSearchQuery_$3() {
	searchQueryElement_$9.style.overflow = "visible";
}
function createCharacterSpanElement_$2(character) {
	const spanElement = document.createElement("span");
	spanElement.textContent = character;
	const defocusBlurAmount = intInRange(CONFIG$8.defocusAmountMin, CONFIG$8.defocusAmountMax);
	const duration = intInRange(CONFIG$8.focusPullDurationMinMs, CONFIG$8.focusPullDurationMaxMs);
	spanElement.style.setProperty("--focus-in-blur", `${defocusBlurAmount}px`);
	spanElement.style.setProperty("--focus-in-duration", `${duration}ms`);
	spanElement.classList.add("search-query-character-focus");
	return spanElement;
}
function stopAnimation_$9() {
	cancelAll$8();
	resetSearchQuery_$8();
}
function appendCharacterSpan_$3(wordSpanElement, character) {
	wordSpanElement.appendChild(createCharacterSpanElement_$2(character));
}
function onSearchQueryComplete_$7(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$9);
	resetSearchQuery_$8();
	onComplete();
}
function animateSearchQuery_$8(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$8();
	unclipSearchQuery_$3();
	const words = splitWords(searchQueryText);
	scheduleCharacterAnimations(words, createWordSpanElements(searchQueryElement_$9, words), CONFIG$8.characterStaggerDelayMs, appendCharacterSpan_$3, scheduleAfter$8);
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG$8.characterStaggerDelayMs + CONFIG$8.focusPullDurationMaxMs, scheduleAfter$8, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$9, contentElement_$9, searchResultImageSrc), () => onSearchQueryComplete_$7(onComplete));
}
/**
* Public.
*/
function init$14({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$9();
	searchQueryElement_$9 = searchQueryElement;
	searchResultImageElement_$9 = searchResultImageElement;
	contentElement_$9 = contentElement;
	simulateTap.init();
}
function simulate$9(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$9, scheduleAfter$8, () => animateSearchQuery_$8(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$9() {
	stopAnimation_$9();
}
var autoTypeFocus = {
	init: init$14,
	simulate: simulate$9,
	cancel: cancel$9
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/neon/config.ts
var CONFIG$7 = {
	characterStaggerDelayMs: 30,
	strikeInDurationMs: 490,
	strikeJitterMaxMs: 30,
	pulseCycleDurationMinMs: 4800,
	pulseCycleDurationMaxMs: 8e3,
	pulseFloorOpacity: .38,
	tubeColor: "rgba(255, 220, 160, 0.92)",
	glowColor: "#FF6000",
	bloomColor: "#cc4d00"
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/neon/index.ts
/**
* Private.
*/
var searchQueryElement_$8;
var searchResultImageElement_$8;
var contentElement_$8;
var { scheduleAfter: scheduleAfter$7, cancelAll: cancelAll$7 } = createScheduler();
function resetSearchQuery_$7() {
	if (!searchQueryElement_$8) return;
	searchQueryElement_$8.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$8.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$8.textContent = "";
}
function createCharacterSpanElement_$1(character, animationDelayJitter) {
	const spanElement = document.createElement("span");
	spanElement.textContent = character;
	spanElement.style.setProperty("--neon-flicker-duration", `${CONFIG$7.strikeInDurationMs}ms`);
	spanElement.style.setProperty("--neon-jitter", `${animationDelayJitter}ms`);
	spanElement.style.setProperty("--neon-core", CONFIG$7.tubeColor);
	spanElement.style.setProperty("--neon-color", CONFIG$7.glowColor);
	spanElement.style.setProperty("--neon-color-dim", CONFIG$7.bloomColor);
	spanElement.style.setProperty("--neon-idle-dropout-opacity", String(CONFIG$7.pulseFloorOpacity));
	spanElement.classList.add("search-query-character-neon");
	return spanElement;
}
function scheduleIdleGlowAnimation_(spanElement, delayMs) {
	const glowDuration = Math.round(CONFIG$7.pulseCycleDurationMinMs + Math.random() * (CONFIG$7.pulseCycleDurationMaxMs - CONFIG$7.pulseCycleDurationMinMs));
	const phaseOffsetMs = -Math.round(Math.random() * glowDuration);
	scheduleAfter$7(() => {
		if (spanElement.isConnected) {
			spanElement.style.opacity = "1";
			spanElement.style.animation = `character-neon-random-glow ${glowDuration}ms ${phaseOffsetMs}ms linear infinite`;
		}
	}, delayMs);
}
function stopAnimation_$8() {
	cancelAll$7();
	resetSearchQuery_$7();
}
function appendCharacterSpan_$2(wordSpanElement, character) {
	const animationDelayJitter = Math.floor(Math.random() * CONFIG$7.strikeJitterMaxMs);
	const spanElement = createCharacterSpanElement_$1(character, animationDelayJitter);
	scheduleIdleGlowAnimation_(spanElement, animationDelayJitter + CONFIG$7.strikeInDurationMs);
	wordSpanElement.appendChild(spanElement);
}
function onSearchQueryComplete_$6(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$8);
	resetSearchQuery_$7();
	onComplete();
}
function animateSearchQuery_$7(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$7();
	const words = splitWords(searchQueryText);
	scheduleCharacterAnimations(words, createWordSpanElements(searchQueryElement_$8, words), CONFIG$7.characterStaggerDelayMs, appendCharacterSpan_$2, scheduleAfter$7);
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG$7.characterStaggerDelayMs + CONFIG$7.strikeInDurationMs + CONFIG$7.strikeJitterMaxMs, scheduleAfter$7, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$8, contentElement_$8, searchResultImageSrc), () => onSearchQueryComplete_$6(onComplete));
}
/**
* Public.
*/
function init$13({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$8();
	searchQueryElement_$8 = searchQueryElement;
	searchResultImageElement_$8 = searchResultImageElement;
	contentElement_$8 = contentElement;
	simulateTap.init();
}
function simulate$8(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$8, scheduleAfter$7, () => animateSearchQuery_$7(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$8() {
	stopAnimation_$8();
}
var autoTypeNeon = {
	init: init$13,
	simulate: simulate$8,
	cancel: cancel$8
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/ghost/config.ts
var CONFIG$6 = {
	characterStaggerDelayMs: 15,
	characterFadeInDurationMs: 600,
	characterFadeInEasing: "cubic-bezier(0.25, 0.46, 0.45, 0.94)"
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/ghost/index.ts
/**
* Private.
*/
var searchQueryElement_$7;
var searchResultImageElement_$7;
var contentElement_$7;
var { scheduleAfter: scheduleAfter$6, cancelAll: cancelAll$6 } = createScheduler();
function resetSearchQuery_$6() {
	if (!searchQueryElement_$7) return;
	searchQueryElement_$7.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$7.style.overflow = "";
	searchQueryElement_$7.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$7.textContent = "";
}
function unclipSearchQuery_$2() {
	searchQueryElement_$7.style.overflow = "visible";
}
function appendCharacterSpan_$1(wordSpanElement, character) {
	const spanElement = document.createElement("span");
	spanElement.textContent = character;
	spanElement.style.setProperty("--ghost-in-duration", `${CONFIG$6.characterFadeInDurationMs}ms`);
	spanElement.classList.add("search-query-character-ghost");
	wordSpanElement.appendChild(spanElement);
}
function stopAnimation_$7() {
	cancelAll$6();
	resetSearchQuery_$6();
}
function onSearchQueryComplete_$5(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$7);
	resetSearchQuery_$6();
	onComplete();
}
function animateSearchQuery_$6(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$6();
	unclipSearchQuery_$2();
	const words = splitWords(searchQueryText);
	scheduleCharacterAnimations(words, createWordSpanElements(searchQueryElement_$7, words), CONFIG$6.characterStaggerDelayMs, appendCharacterSpan_$1, scheduleAfter$6);
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG$6.characterStaggerDelayMs + CONFIG$6.characterFadeInDurationMs, scheduleAfter$6, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$7, contentElement_$7, searchResultImageSrc), () => onSearchQueryComplete_$5(onComplete));
}
/**
* Public.
*/
function init$12({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$7();
	searchQueryElement_$7 = searchQueryElement;
	searchQueryElement_$7.style.setProperty("--ghost-in-easing", CONFIG$6.characterFadeInEasing);
	searchResultImageElement_$7 = searchResultImageElement;
	contentElement_$7 = contentElement;
	simulateTap.init();
}
function simulate$7(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$7, scheduleAfter$6, () => animateSearchQuery_$6(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$7() {
	stopAnimation_$7();
}
var autoTypeGhost = {
	init: init$12,
	simulate: simulate$7,
	cancel: cancel$7
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/waterfall/config.ts
var CONFIG$5 = {
	characterStaggerDelayMs: 25,
	fallDurationMinMs: 200,
	fallDurationMaxMs: 400,
	fallHeightMin: -50,
	fallHeightMax: -15,
	trailDurationMinMs: 180,
	trailDurationMaxMs: 320,
	trailDelayMinMs: 60,
	trailDelayMaxMs: 140
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/waterfall/index.ts
/**
* Private.
*/
var searchQueryElement_$6;
var searchResultImageElement_$6;
var contentElement_$6;
var { scheduleAfter: scheduleAfter$5, cancelAll: cancelAll$5 } = createScheduler();
function resetSearchQuery_$5() {
	if (!searchQueryElement_$6) return;
	searchQueryElement_$6.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$6.style.overflow = "";
	searchQueryElement_$6.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$6.textContent = "";
}
function unclipSearchQuery_$1() {
	searchQueryElement_$6.style.overflow = "visible";
}
function createFallingCharacterElement_(character) {
	const fromY = intInRange(CONFIG$5.fallHeightMin, CONFIG$5.fallHeightMax);
	const fallDuration = intInRange(CONFIG$5.fallDurationMinMs, CONFIG$5.fallDurationMaxMs);
	const trailDuration = intInRange(CONFIG$5.trailDurationMinMs, CONFIG$5.trailDurationMaxMs);
	const trailDelay = intInRange(CONFIG$5.trailDelayMinMs, CONFIG$5.trailDelayMaxMs);
	const primarySpanElement = document.createElement("span");
	primarySpanElement.classList.add("search-query-character-waterfall");
	primarySpanElement.textContent = character;
	primarySpanElement.style.setProperty("--waterfall-from-y", `${fromY}px`);
	primarySpanElement.style.setProperty("--waterfall-fall-duration", `${fallDuration}ms`);
	const ghostSpanElement = document.createElement("span");
	ghostSpanElement.classList.add("search-query-character-waterfall-ghost");
	ghostSpanElement.textContent = character;
	ghostSpanElement.style.setProperty("--waterfall-from-y", `${fromY}px`);
	ghostSpanElement.style.setProperty("--waterfall-trail-duration", `${trailDuration}ms`);
	ghostSpanElement.style.setProperty("--waterfall-trail-delay", `${trailDelay}ms`);
	const characterElement = document.createElement("span");
	characterElement.classList.add("search-query-waterfall-character");
	characterElement.appendChild(primarySpanElement);
	characterElement.appendChild(ghostSpanElement);
	return characterElement;
}
function appendCharacterSpan_(wordSpanElement, character) {
	wordSpanElement.appendChild(createFallingCharacterElement_(character));
}
function stopAnimation_$6() {
	cancelAll$5();
	resetSearchQuery_$5();
}
function onSearchQueryComplete_$4(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$6);
	resetSearchQuery_$5();
	onComplete();
}
function animateSearchQuery_$5(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$5();
	unclipSearchQuery_$1();
	const words = splitWords(searchQueryText);
	scheduleCharacterAnimations(words, createWordSpanElements(searchQueryElement_$6, words), CONFIG$5.characterStaggerDelayMs, appendCharacterSpan_, scheduleAfter$5);
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG$5.characterStaggerDelayMs + CONFIG$5.fallDurationMaxMs, scheduleAfter$5, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$6, contentElement_$6, searchResultImageSrc), () => onSearchQueryComplete_$4(onComplete));
}
/**
* Public.
*/
function init$11({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$6();
	searchQueryElement_$6 = searchQueryElement;
	searchResultImageElement_$6 = searchResultImageElement;
	contentElement_$6 = contentElement;
	simulateTap.init();
}
function simulate$6(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$6, scheduleAfter$5, () => animateSearchQuery_$5(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$6() {
	stopAnimation_$6();
}
var autoTypeWaterfall = {
	init: init$11,
	simulate: simulate$6,
	cancel: cancel$6
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/assemble/config.ts
var CONFIG$4 = {
	flyDistanceMin: 40,
	flyDistanceMax: 1500,
	flyDurationMs: 500,
	flyDurationJitterMinMs: 60,
	flyDurationJitterMaxMs: 80,
	characterSettleDurationMs: 80,
	characterStaggerDelayMs: 20,
	characterStartScaleMin: 8,
	characterStartScaleMax: 32,
	characterEndScaleMin: 1.05,
	characterEndScaleMax: 1.15,
	characterStartRotationMax: 180,
	characterFlyEasing: "cubic-bezier(0.16, 1, 0.3, 1)"
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/assemble/index.ts
/**
* Private.
*/
var searchQueryElement_$5;
var searchResultImageElement_$5;
var contentElement_$5;
var { scheduleAfter: scheduleAfter$4, cancelAll: cancelAll$4 } = createScheduler();
function unclipSearchQuery_() {
	searchQueryElement_$5.style.overflow = "visible";
}
function resetSearchQuery_$4() {
	if (!searchQueryElement_$5) return;
	searchQueryElement_$5.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$5.style.overflow = "";
	searchQueryElement_$5.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$5.textContent = "";
	setSearchContainerOverlay_(false);
}
function setSearchContainerOverlay_(active) {
	const searchContainerElement = searchQueryElement_$5.closest(".search-container");
	if (!searchContainerElement) return;
	searchContainerElement.style.position = active ? "relative" : "";
	searchContainerElement.style.zIndex = active ? "10" : "";
}
function createCharacterSpanElement_(character) {
	const spanElement = document.createElement("span");
	spanElement.textContent = character;
	const angle = Math.random() * 2 * Math.PI;
	const distance = intInRange(CONFIG$4.flyDistanceMin, CONFIG$4.flyDistanceMax);
	const fromX = Math.round(Math.cos(angle) * distance);
	const fromY = Math.round(Math.sin(angle) * distance);
	const duration = intInRange(CONFIG$4.flyDurationMs - CONFIG$4.flyDurationJitterMinMs, CONFIG$4.flyDurationMs + CONFIG$4.flyDurationJitterMaxMs);
	const startScale = (CONFIG$4.characterStartScaleMin + Math.random() * (CONFIG$4.characterStartScaleMax - CONFIG$4.characterStartScaleMin)).toFixed(3);
	const endScale = (CONFIG$4.characterEndScaleMin + Math.random() * (CONFIG$4.characterEndScaleMax - CONFIG$4.characterEndScaleMin)).toFixed(3);
	const rotation = Math.round((Math.random() * 2 - 1) * CONFIG$4.characterStartRotationMax);
	spanElement.style.setProperty("--assemble-fly-from-x", `${fromX}px`);
	spanElement.style.setProperty("--assemble-fly-from-y", `${fromY}px`);
	spanElement.style.setProperty("--assemble-fly-duration", `${duration}ms`);
	spanElement.style.setProperty("--assemble-character-start-scale", startScale);
	spanElement.style.setProperty("--assemble-character-end-scale", endScale);
	spanElement.style.setProperty("--assemble-character-start-rotation", `${rotation}deg`);
	spanElement.style.opacity = "0";
	return spanElement;
}
function startCharacterAnimation_(spanElement) {
	spanElement.classList.add("search-query-character-assemble");
}
function stopAnimation_$5() {
	cancelAll$4();
	resetSearchQuery_$4();
}
function onSearchQueryComplete_$3(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$5);
	resetSearchQuery_$4();
	onComplete();
}
function animateSearchQuery_$4(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$4();
	unclipSearchQuery_();
	setSearchContainerOverlay_(true);
	const words = splitWords(searchQueryText);
	const wordSpanElements = createWordSpanElements(searchQueryElement_$5, words);
	const characterSpanElements = [];
	words.forEach((word, wordIndex) => {
		for (const character of word) {
			const spanElement = createCharacterSpanElement_(character);
			wordSpanElements[wordIndex].appendChild(spanElement);
			characterSpanElements.push(spanElement);
		}
	});
	shuffledIndices(characterSpanElements.length).forEach((spanIndex, step) => {
		scheduleAfter$4(() => startCharacterAnimation_(characterSpanElements[spanIndex]), step * CONFIG$4.characterStaggerDelayMs);
	});
	scheduleAnimationComplete((characterSpanElements.length - 1) * CONFIG$4.characterStaggerDelayMs + CONFIG$4.flyDurationMs + CONFIG$4.characterSettleDurationMs, scheduleAfter$4, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$5, contentElement_$5, searchResultImageSrc), () => onSearchQueryComplete_$3(onComplete));
}
/**
* Public.
*/
function init$10({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$5();
	searchQueryElement_$5 = searchQueryElement;
	searchQueryElement_$5.style.setProperty("--assemble-character-fly-easing", CONFIG$4.characterFlyEasing);
	searchResultImageElement_$5 = searchResultImageElement;
	contentElement_$5 = contentElement;
	simulateTap.init();
}
function simulate$5(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$5, scheduleAfter$4, () => animateSearchQuery_$4(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$5() {
	stopAnimation_$5();
}
var autoTypeAssemble = {
	init: init$10,
	simulate: simulate$5,
	cancel: cancel$5
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/sprinkle/config.ts
var CONFIG$3 = {
	characterStaggerDelayMs: 20,
	characterFadeInDurationMs: 250
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/sprinkle/index.ts
/**
* Private.
*/
var searchQueryElement_$4;
var searchResultImageElement_$4;
var contentElement_$4;
var { scheduleAfter: scheduleAfter$3, cancelAll: cancelAll$3 } = createScheduler();
function resetSearchQuery_$3() {
	if (!searchQueryElement_$4) return;
	searchQueryElement_$4.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$4.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$4.textContent = "";
}
function createCharacterSpanElements_(words, wordSpanElements) {
	const characterSpanElements = [];
	words.forEach((word, wordIndex) => {
		for (const character of word) {
			const spanElement = document.createElement("span");
			spanElement.textContent = character;
			spanElement.style.setProperty("--sprinkle-fade-duration", `${CONFIG$3.characterFadeInDurationMs}ms`);
			spanElement.style.opacity = "0";
			characterSpanElements.push(spanElement);
			wordSpanElements[wordIndex].appendChild(spanElement);
		}
	});
	return characterSpanElements;
}
function scheduleCharacterReveals_(characterSpanElements) {
	shuffledIndices(characterSpanElements.length).forEach((spanIndex, revealStep) => {
		scheduleAfter$3(() => {
			characterSpanElements[spanIndex].classList.add("search-query-character-sprinkle");
		}, revealStep * CONFIG$3.characterStaggerDelayMs);
	});
}
function stopAnimation_$4() {
	cancelAll$3();
	resetSearchQuery_$3();
}
function onSearchQueryComplete_$2(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$4);
	resetSearchQuery_$3();
	onComplete();
}
function animateSearchQuery_$3(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$3();
	const words = splitWords(searchQueryText);
	const characterSpanElements = createCharacterSpanElements_(words, createWordSpanElements(searchQueryElement_$4, words));
	scheduleCharacterReveals_(characterSpanElements);
	scheduleAnimationComplete((characterSpanElements.length - 1) * CONFIG$3.characterStaggerDelayMs + CONFIG$3.characterFadeInDurationMs, scheduleAfter$3, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$4, contentElement_$4, searchResultImageSrc), () => onSearchQueryComplete_$2(onComplete));
}
/**
* Public.
*/
function init$9({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$4();
	searchQueryElement_$4 = searchQueryElement;
	searchResultImageElement_$4 = searchResultImageElement;
	contentElement_$4 = contentElement;
	simulateTap.init();
}
function simulate$4(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$4, scheduleAfter$3, () => animateSearchQuery_$3(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$4() {
	stopAnimation_$4();
}
var autoTypeSprinkle = {
	init: init$9,
	simulate: simulate$4,
	cancel: cancel$4
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/reduced-motion/config.ts
var CONFIG$2 = {
	nextSearchResultQueryAfterMs: 5e3,
	placeholderFadeAfterMs: 1e3
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/reduced-motion/index.ts
/**
* Private.
*/
var searchQueryElement_$3;
var searchResultImageElement_$3;
var contentElement_$3;
var { scheduleAfter: scheduleAfter$2, cancelAll: cancelAll$2 } = createScheduler();
var isFirstQuery_ = true;
function resetSearchQuery_$2() {
	isFirstQuery_ = true;
	if (!searchQueryElement_$3) return;
	searchQueryElement_$3.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$3.textContent = "";
}
function stopAnimation_$3() {
	cancelAll$2();
	resetSearchQuery_$2();
}
function animateSearchQuery_$2(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	searchQueryElement_$3.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$3.textContent = searchQueryText;
	searchQueryElement_$3.style.color = autoTypeConfig.searchQueryTextColor;
	showSearchResult(searchResultImageElement_$3, contentElement_$3, searchResultImageSrc);
	scheduleAfter$2(() => {
		hideSearchResult$1(searchResultImageElement_$3);
		onComplete();
	}, CONFIG$2.nextSearchResultQueryAfterMs - simulateTapConfig.tapButtonAnimationDuration * autoTypeConfig.searchResultAppearsFactor);
}
/**
* Public.
*/
function init$8({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$3();
	searchQueryElement_$3 = searchQueryElement;
	searchResultImageElement_$3 = searchResultImageElement;
	contentElement_$3 = contentElement;
	searchResultImageElement_$3.style.transition = "none";
	simulateTap.init();
}
function simulate$3(searchQueryText, searchResultImageSrc, onComplete) {
	if (isFirstQuery_) {
		isFirstQuery_ = false;
		scheduleAfter$2(() => animateSearchQuery_$2(searchQueryText, searchResultImageSrc, onComplete), prefersReducedTransparency ? 0 : CONFIG$2.placeholderFadeAfterMs);
	} else animateSearchQuery_$2(searchQueryText, searchResultImageSrc, onComplete);
}
function cancel$3() {
	stopAnimation_$3();
}
var autoTypeReducedMotion = {
	init: init$8,
	simulate: simulate$3,
	cancel: cancel$3
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/magnify/config.ts
var CONFIG$1 = {
	lensRadius: 115,
	lensYOffset: -20,
	magnificationFactor: 1.5,
	barrelDistortionStrength: .33,
	specularShininess: 192,
	specularIntensity: .55,
	causticIntensity: .06,
	chromaticAberration: .015,
	fresnelIntensity: .2,
	scanDurationMs: 3500,
	lensFadeDurationMs: 600,
	cipherRefreshIntervalMs: 55,
	cipherFadeDurationMs: 300
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/magnify/lens-source.ts
/**
* Private.
*/
function isVisible_(element) {
	const elementClientRect = element.getBoundingClientRect();
	return elementClientRect.width > 0 && elementClientRect.height > 0;
}
function drawElementBackground_(canvasRenderingContext, element, referenceRect) {
	const style = window.getComputedStyle(element);
	const backgroundColor = style.backgroundColor;
	if (!backgroundColor || backgroundColor === "rgba(0, 0, 0, 0)" || backgroundColor === "transparent") return;
	const elementClientRect = element.getBoundingClientRect();
	canvasRenderingContext.fillStyle = backgroundColor;
	canvasRenderingContext.beginPath();
	canvasRenderingContext.roundRect(elementClientRect.left - referenceRect.left, elementClientRect.top - referenceRect.top, elementClientRect.width, elementClientRect.height, parseFloat(style.borderRadius) || 0);
	canvasRenderingContext.fill();
}
function drawElementText_(canvasRenderingContext, element, referenceRect) {
	const text = element.textContent?.trim();
	if (!text) return;
	const style = window.getComputedStyle(element);
	const elementClientRect = element.getBoundingClientRect();
	const x = elementClientRect.left - referenceRect.left;
	const y = elementClientRect.top - referenceRect.top;
	canvasRenderingContext.font = style.font;
	canvasRenderingContext.fillStyle = style.color;
	canvasRenderingContext.textAlign = style.textAlign;
	canvasRenderingContext.textBaseline = "middle";
	const textX = style.textAlign === "center" ? x + elementClientRect.width / 2 : style.textAlign === "right" ? x + elementClientRect.width : x + (parseFloat(style.paddingLeft) || 0);
	canvasRenderingContext.fillText(text, textX, y + elementClientRect.height / 2);
}
function drawImgElements_(canvasRenderingContext, referenceRect, imgElements) {
	for (const imgElement of imgElements) {
		if (!imgElement.complete || !imgElement.naturalWidth) continue;
		const imgElementClientRect = imgElement.getBoundingClientRect();
		const destX = imgElementClientRect.left - referenceRect.left;
		const destY = imgElementClientRect.top - referenceRect.top;
		try {
			canvasRenderingContext.drawImage(imgElement, destX, destY, imgElementClientRect.width, imgElementClientRect.height);
		} catch {}
	}
}
function drawCssBackgroundImageElements_(canvasRenderingContext, referenceRect, searchBoxElements) {
	for (const searchBoxElement of searchBoxElements) {
		const searchBoxElementStyle = window.getComputedStyle(searchBoxElement);
		const backgroundImage = searchBoxElementStyle.backgroundImage;
		if (!backgroundImage || backgroundImage === "none") continue;
		const url = backgroundImage.match(/url\(["']?([^"')]+)["']?\)/)?.[1];
		if (!url) continue;
		const backgroundImgElement = new Image();
		backgroundImgElement.src = url;
		if (!backgroundImgElement.complete || !backgroundImgElement.naturalWidth) continue;
		const searchBoxElementClientRect = searchBoxElement.getBoundingClientRect();
		const elementX = searchBoxElementClientRect.left - referenceRect.left;
		const elementY = searchBoxElementClientRect.top - referenceRect.top;
		const [cssBackgroundWidth, cssBackgroundHeight] = searchBoxElementStyle.backgroundSize.split(" ");
		const backgroundImageDrawWidth = cssBackgroundWidth === "auto" ? backgroundImgElement.naturalWidth : parseFloat(cssBackgroundWidth);
		const backgroundImageDrawHeight = cssBackgroundHeight && cssBackgroundHeight !== "auto" ? parseFloat(cssBackgroundHeight) : backgroundImgElement.naturalHeight;
		if (!backgroundImageDrawWidth || !backgroundImageDrawHeight) continue;
		const [cssBackgroundPositionX, cssBackgroundPositionY = "center"] = searchBoxElementStyle.backgroundPosition.split(" ");
		let backgroundX;
		if (cssBackgroundPositionX === "right") backgroundX = elementX + searchBoxElementClientRect.width - backgroundImageDrawWidth;
		else if (cssBackgroundPositionX.endsWith("%")) backgroundX = elementX + parseFloat(cssBackgroundPositionX) / 100 * (searchBoxElementClientRect.width - backgroundImageDrawWidth);
		else if (cssBackgroundPositionX === "center") backgroundX = elementX + (searchBoxElementClientRect.width - backgroundImageDrawWidth) / 2;
		else backgroundX = elementX + (parseFloat(cssBackgroundPositionX) || 0);
		let backgroundY;
		if (cssBackgroundPositionY === "bottom") backgroundY = elementY + searchBoxElementClientRect.height - backgroundImageDrawHeight;
		else if (cssBackgroundPositionY.endsWith("%")) backgroundY = elementY + parseFloat(cssBackgroundPositionY) / 100 * (searchBoxElementClientRect.height - backgroundImageDrawHeight);
		else if (cssBackgroundPositionY === "center") backgroundY = elementY + (searchBoxElementClientRect.height - backgroundImageDrawHeight) / 2;
		else backgroundY = elementY + (parseFloat(cssBackgroundPositionY) || 0);
		try {
			canvasRenderingContext.drawImage(backgroundImgElement, backgroundX, backgroundY, backgroundImageDrawWidth, backgroundImageDrawHeight);
		} catch {}
	}
}
function wrapTextToLines_(canvasRenderingContext, searchQueryText, maxLineWidth) {
	const words = searchQueryText.split(" ");
	const lines = [];
	let lineText = "";
	for (const word of words) {
		const lineWithWord = lineText ? `${lineText} ${word}` : word;
		if (canvasRenderingContext.measureText(lineWithWord).width > maxLineWidth && lineText) {
			lines.push(lineText);
			lineText = word;
		} else lineText = lineWithWord;
	}
	if (lineText) lines.push(lineText);
	return lines;
}
function drawSearchQueryText_(canvasRenderingContext, searchQueryLayerContainerElement, referenceRect, searchQueryText) {
	const searchQueryLayerContainerClientRect = searchQueryLayerContainerElement.getBoundingClientRect();
	const searchQueryLayerContainerWidth = searchQueryLayerContainerElement.offsetWidth;
	const searchQueryLayerContainerHeight = searchQueryLayerContainerElement.offsetHeight;
	const textOffsetX = searchQueryLayerContainerClientRect.left - referenceRect.left;
	const textOffsetY = searchQueryLayerContainerClientRect.top - referenceRect.top;
	const computedStyle = window.getComputedStyle(searchQueryLayerContainerElement);
	canvasRenderingContext.font = computedStyle.font;
	canvasRenderingContext.fillStyle = autoTypeConfig.searchQueryTextColor;
	canvasRenderingContext.textAlign = computedStyle.textAlign;
	canvasRenderingContext.textBaseline = "middle";
	const textX = computedStyle.textAlign === "center" ? textOffsetX + searchQueryLayerContainerWidth / 2 : textOffsetX;
	const lines = wrapTextToLines_(canvasRenderingContext, searchQueryText, searchQueryLayerContainerWidth);
	const lineHeight = computedStyle.lineHeight === "normal" ? parseFloat(computedStyle.fontSize) * 1.2 : parseFloat(computedStyle.lineHeight);
	const startY = textOffsetY + (searchQueryLayerContainerHeight - lines.length * lineHeight) / 2 + lineHeight / 2;
	for (let i = 0; i < lines.length; i++) canvasRenderingContext.fillText(lines[i], textX, startY + i * lineHeight);
}
/**
* Public.
*/
function prepareLensSourceCanvas(contentElement, searchQueryLayerContainerElement, searchQueryText, searchResultImageSrc) {
	const contentClientRect = contentElement.getBoundingClientRect();
	const contentWidth = Math.round(contentClientRect.width);
	const contentHeight = Math.max(Math.round(contentClientRect.height), contentElement.scrollHeight);
	if (!contentWidth || !contentHeight) return null;
	const devicePixelRatio = window.devicePixelRatio || 1;
	const resultImgElement = searchResultImageSrc ? contentElement.querySelector(".search-result-image") : null;
	const resultImageVisible = resultImgElement?.classList.contains("visible") ?? false;
	let extraHeight = 0;
	if (resultImageVisible && resultImgElement?.complete && resultImgElement.naturalWidth) {
		const resultImageTop = resultImgElement.getBoundingClientRect().top - contentClientRect.top;
		const resultImageDrawHeight = contentWidth * resultImgElement.naturalHeight / resultImgElement.naturalWidth;
		extraHeight = Math.max(0, resultImageTop + resultImageDrawHeight - contentHeight);
	}
	const physicalWidth = Math.round(contentWidth * devicePixelRatio);
	const physicalHeight = Math.round((contentHeight + extraHeight) * devicePixelRatio);
	const sourceCanvas = new OffscreenCanvas(physicalWidth, physicalHeight);
	const canvasRenderingContext = sourceCanvas.getContext("2d");
	if (!canvasRenderingContext) return null;
	canvasRenderingContext.scale(devicePixelRatio, devicePixelRatio);
	const pageBackground = window.getComputedStyle(document.body).backgroundColor;
	if (pageBackground && pageBackground !== "rgba(0, 0, 0, 0)" && pageBackground !== "transparent") {
		canvasRenderingContext.fillStyle = pageBackground;
		canvasRenderingContext.fillRect(0, 0, contentWidth, contentHeight + extraHeight);
	}
	if (resultImageVisible && resultImgElement?.complete && resultImgElement.naturalWidth) {
		const resultImgElementClientRect = resultImgElement.getBoundingClientRect();
		const destX = resultImgElementClientRect.left - contentClientRect.left;
		const destY = resultImgElementClientRect.top - contentClientRect.top;
		const destWidth = resultImgElementClientRect.width;
		const destHeight = destWidth * resultImgElement.naturalHeight / resultImgElement.naturalWidth;
		try {
			canvasRenderingContext.drawImage(resultImgElement, destX, destY, destWidth, destHeight);
		} catch {}
	}
	const searchBoxElement = contentElement.querySelector(".search-box");
	if (searchBoxElement) drawElementBackground_(canvasRenderingContext, searchBoxElement, contentClientRect);
	drawImgElements_(canvasRenderingContext, contentClientRect, contentElement.querySelectorAll("img:not(.search-result-image)"));
	const searchBoxIconButtonElement = searchBox.getSearchBoxIconButtonElement();
	drawCssBackgroundImageElements_(canvasRenderingContext, contentClientRect, searchBoxIconButtonElement ? [searchBoxIconButtonElement] : []);
	const taglineElement = contentElement.querySelector("#tagline");
	if (taglineElement && isVisible_(taglineElement)) drawElementText_(canvasRenderingContext, taglineElement, contentClientRect);
	const subHeadlineElement = contentElement.querySelector("#sub-headline");
	if (subHeadlineElement && isVisible_(subHeadlineElement)) drawElementText_(canvasRenderingContext, subHeadlineElement, contentClientRect);
	const tryNowButtonElement = contentElement.querySelector("#try-now-button");
	if (tryNowButtonElement && isVisible_(tryNowButtonElement)) {
		drawElementBackground_(canvasRenderingContext, tryNowButtonElement, contentClientRect);
		drawElementText_(canvasRenderingContext, tryNowButtonElement, contentClientRect);
	}
	const makeDefaultButtonElement = contentElement.querySelector("#make-default-button");
	if (makeDefaultButtonElement && isVisible_(makeDefaultButtonElement)) {
		drawElementBackground_(canvasRenderingContext, makeDefaultButtonElement, contentClientRect);
		drawElementText_(canvasRenderingContext, makeDefaultButtonElement, contentClientRect);
	}
	drawSearchQueryText_(canvasRenderingContext, searchQueryLayerContainerElement, contentClientRect, searchQueryText);
	return sourceCanvas;
}
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/magnify/webgl-lens.ts
/**
* Private.
*/
var VERTEX_SHADER_SOURCE = `
  attribute vec2 aPosition;
  void main() {
    gl_Position = vec4(aPosition, 0.0, 1.0);
  }
`;
var FRAGMENT_SHADER_SOURCE = `
  precision mediump float;
  uniform sampler2D uSource;
  uniform float uDevicePixelRatio;
  uniform float uPhysicalDiameter;
  uniform float uLensRadius;
  uniform float uLensX;
  uniform float uLensY;
  uniform float uMagnificationFactor;
  uniform float uBarrelStrength;
  uniform vec2 uSourcePhysicalSize;
  uniform float uScanProgress;
  uniform float uSpecularIntensity;
  uniform float uSpecularShininess;
  uniform float uCausticIntensity;
  uniform vec3 uBackgroundColor;
  uniform float uChromaticAberration;
  uniform float uFresnelIntensity;

  void main() {
    // gl_FragCoord is bottom-up; convert to top-down screen pixels.
    float screenX = gl_FragCoord.x;
    float screenY = uPhysicalDiameter - gl_FragCoord.y;

    // CSS pixel offset from the lens center.
    float cssDeltaX = screenX / uDevicePixelRatio - uLensRadius;
    float cssDeltaY = screenY / uDevicePixelRatio - uLensRadius;

    // Normalize to the unit circle.
    float normalizedX = cssDeltaX / uLensRadius;
    float normalizedY = cssDeltaY / uLensRadius;
    float distSq = normalizedX * normalizedX + normalizedY * normalizedY;

    if (distSq > 1.0) {
      gl_FragColor = vec4(0.0);
      return;
    }

    float dist = sqrt(distSq);

    // Barrel distortion reduces magnification toward the edge, matching a real
    // converging lens.
    float barrelFactor = 1.0 + uBarrelStrength * distSq;
    float nominalScale = barrelFactor / uMagnificationFactor;
    float sourceNormX = normalizedX * nominalScale;
    float sourceNormY = normalizedY * nominalScale;

    // Map to source canvas CSS coordinates, then to physical pixels.
    float srcCssX = uLensX + sourceNormX * uLensRadius;
    float srcCssY = uLensY + sourceNormY * uLensRadius;
    float srcUvX = srcCssX * uDevicePixelRatio / uSourcePhysicalSize.x;
    float srcUvY = srcCssY * uDevicePixelRatio / uSourcePhysicalSize.y;

    if (srcUvX < 0.0 || srcUvX > 1.0 || srcUvY < 0.0 || srcUvY > 1.0) {
      gl_FragColor = vec4(uBackgroundColor, 1.0);
      return;
    }

    vec4 color = texture2D(uSource, vec2(srcUvX, srcUvY));

    // Chromatic aberration: red bends less through glass than blue, so the red
    // channel is sampled slightly inward and blue slightly outward. The split
    // scales linearly with distance from center so the lens interior stays clean
    // and fringing only appears near the rim.
    float chromaticAberration = dist * uChromaticAberration;
    float rScale = nominalScale * (1.0 - chromaticAberration);
    float bScale = nominalScale * (1.0 + chromaticAberration);

    float rUvX = (uLensX + normalizedX * rScale * uLensRadius) * uDevicePixelRatio / uSourcePhysicalSize.x;
    float rUvY = (uLensY + normalizedY * rScale * uLensRadius) * uDevicePixelRatio / uSourcePhysicalSize.y;
    float bUvX = (uLensX + normalizedX * bScale * uLensRadius) * uDevicePixelRatio / uSourcePhysicalSize.x;
    float bUvY = (uLensY + normalizedY * bScale * uLensRadius) * uDevicePixelRatio / uSourcePhysicalSize.y;

    float r = (rUvX >= 0.0 && rUvX <= 1.0 && rUvY >= 0.0 && rUvY <= 1.0)
        ? texture2D(uSource, vec2(rUvX, rUvY)).r : color.r;
    float b = (bUvX >= 0.0 && bUvX <= 1.0 && bUvY >= 0.0 && bUvY <= 1.0)
        ? texture2D(uSource, vec2(bUvX, bUvY)).b : color.b;
    color = vec4(r, color.g, b, color.a);

    // Hemisphere surface normal at this pixel. Z is the depth of the sphere
    // surface above the lens plane, giving a smooth outward-pointing normal.
    vec3 N = normalize(vec3(normalizedX, normalizedY, sqrt(max(0.0, 1.0 - distSq))));

    // Viewer direction is straight out of the screen.
    vec3 V = vec3(0.0, 0.0, 1.0);

    // Light azimuth drifts from right to left as the lens scans left to right,
    // simulating a fixed overhead light source reflecting off the glass.
    float lightX = mix(0.5, -0.5, uScanProgress);
    vec3 L = normalize(vec3(lightX, -0.7, 1.0));

    // Blinn-Phong specular highlight.
    vec3 H = normalize(L + V);
    float specular = pow(max(dot(N, H), 0.0), uSpecularShininess) * uSpecularIntensity;

    // Caustic rim: bright ring near the lens edge from edge refraction.
    float caustic = smoothstep(0.65, 0.80, dist) * smoothstep(1.0, 0.80, dist) * uCausticIntensity;

    // Fresnel: glass edges are more reflective at grazing angles (Schlick
    // approximation). N.z equals cos(angle from normal), so 1.0 - N.z is 0 at
    // the lens center and approaches 1.0 at the rim.
    float fresnel = pow(1.0 - N.z, 3.0) * uFresnelIntensity;

    float lighting = specular + caustic + fresnel;
    gl_FragColor = vec4(color.rgb + lighting, color.a);
  }
`;
var lensState_ = null;
function compileShader_(gl, source, type) {
	const shader = gl.createShader(type);
	if (!shader) return null;
	gl.shaderSource(shader, source);
	gl.compileShader(shader);
	if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
		gl.deleteShader(shader);
		return null;
	}
	return shader;
}
/**
* Public.
*/
function initWebGLLens(canvas, backgroundColor) {
	const gl = canvas.getContext("webgl");
	if (!gl) return false;
	const vertexShader = compileShader_(gl, VERTEX_SHADER_SOURCE, gl.VERTEX_SHADER);
	const fragmentShader = compileShader_(gl, FRAGMENT_SHADER_SOURCE, gl.FRAGMENT_SHADER);
	if (!vertexShader || !fragmentShader) return false;
	const program = gl.createProgram();
	if (!program) return false;
	gl.attachShader(program, vertexShader);
	gl.attachShader(program, fragmentShader);
	gl.linkProgram(program);
	gl.deleteShader(vertexShader);
	gl.deleteShader(fragmentShader);
	if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return false;
	gl.useProgram(program);
	const buffer = gl.createBuffer();
	gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
	gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
		-1,
		-1,
		1,
		-1,
		-1,
		1,
		1,
		1
	]), gl.STATIC_DRAW);
	const aPosition = gl.getAttribLocation(program, "aPosition");
	gl.enableVertexAttribArray(aPosition);
	gl.vertexAttribPointer(aPosition, 2, gl.FLOAT, false, 0, 0);
	const texture = gl.createTexture();
	if (!texture) return false;
	gl.bindTexture(gl.TEXTURE_2D, texture);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
	gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
	gl.uniform1i(gl.getUniformLocation(program, "uSource"), 0);
	gl.uniform1f(gl.getUniformLocation(program, "uLensRadius"), CONFIG$1.lensRadius);
	gl.uniform1f(gl.getUniformLocation(program, "uMagnificationFactor"), CONFIG$1.magnificationFactor);
	gl.uniform1f(gl.getUniformLocation(program, "uBarrelStrength"), CONFIG$1.barrelDistortionStrength);
	gl.uniform1f(gl.getUniformLocation(program, "uSpecularIntensity"), CONFIG$1.specularIntensity);
	gl.uniform1f(gl.getUniformLocation(program, "uSpecularShininess"), CONFIG$1.specularShininess);
	gl.uniform1f(gl.getUniformLocation(program, "uCausticIntensity"), CONFIG$1.causticIntensity);
	gl.uniform3fv(gl.getUniformLocation(program, "uBackgroundColor"), backgroundColor);
	gl.uniform1f(gl.getUniformLocation(program, "uChromaticAberration"), CONFIG$1.chromaticAberration);
	gl.uniform1f(gl.getUniformLocation(program, "uFresnelIntensity"), CONFIG$1.fresnelIntensity);
	const uDevicePixelRatio = gl.getUniformLocation(program, "uDevicePixelRatio");
	const uPhysicalDiameter = gl.getUniformLocation(program, "uPhysicalDiameter");
	const uLensX = gl.getUniformLocation(program, "uLensX");
	const uLensY = gl.getUniformLocation(program, "uLensY");
	const uSourcePhysicalSize = gl.getUniformLocation(program, "uSourcePhysicalSize");
	const uScanProgress = gl.getUniformLocation(program, "uScanProgress");
	if (!uDevicePixelRatio || !uPhysicalDiameter || !uLensX || !uLensY || !uSourcePhysicalSize || !uScanProgress) return false;
	lensState_ = {
		gl,
		program,
		texture,
		uniforms: {
			uDevicePixelRatio,
			uPhysicalDiameter,
			uLensX,
			uLensY,
			uSourcePhysicalSize,
			uScanProgress
		}
	};
	return true;
}
function renderWebGLLens(sourceCanvas, lensX, lensY, devicePixelRatio, physicalDiameter, scanProgress) {
	if (!lensState_) return;
	const { gl, texture, uniforms } = lensState_;
	gl.bindTexture(gl.TEXTURE_2D, texture);
	gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, sourceCanvas);
	gl.uniform1f(uniforms.uDevicePixelRatio, devicePixelRatio);
	gl.uniform1f(uniforms.uPhysicalDiameter, physicalDiameter);
	gl.uniform1f(uniforms.uLensX, lensX);
	gl.uniform1f(uniforms.uLensY, lensY);
	gl.uniform2f(uniforms.uSourcePhysicalSize, sourceCanvas.width, sourceCanvas.height);
	gl.uniform1f(uniforms.uScanProgress, scanProgress);
	gl.viewport(0, 0, physicalDiameter, physicalDiameter);
	gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}
function destroyWebGLLens() {
	if (!lensState_) return;
	const { gl, program, texture } = lensState_;
	gl.deleteTexture(texture);
	gl.deleteProgram(program);
	gl.getExtension("WEBGL_lose_context")?.loseContext();
	lensState_ = null;
}
//#endregion
//#region ../../apis/color.ts
function parseCssColor(cssColor) {
	const match = cssColor.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
	if (!match) {
		debugLog(`Failed to parse CSS color ${cssColor}.`);
		return [
			0,
			0,
			0
		];
	}
	const [, red, green, blue] = match;
	return [
		parseInt(red, 10),
		parseInt(green, 10),
		parseInt(blue, 10)
	];
}
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/magnify/index.ts
/**
* Private.
*/
var CIPHER_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
var searchQueryElement_$2;
var searchResultImageElement_$2;
var contentElement_$2;
var { scheduleAfter: scheduleAfter$1, cancelAll: cancelAll$1 } = createScheduler();
var cancelMagnifyingGlassScan_ = null;
var cipherIntervalId_ = null;
var startAnimationFrameId_ = null;
var magnifyingGlassFadeTimeoutId_ = null;
var lensElement_ = null;
var lensBezelElement_ = null;
var searchQueryCipherLayerSpanElement_ = null;
var searchQueryLayerSpanElement_ = null;
var scanContainerWidth_ = 0;
var scanContainerOffsetX_ = 0;
var magnifyingGlassYInContent_ = 0;
function measureLines_(layoutElement, layoutText) {
	const textNode = layoutElement.firstChild;
	if (!textNode || textNode.nodeType !== Node.TEXT_NODE) return {
		breakPositions: [],
		lineWidths: []
	};
	const range = document.createRange();
	const breakPositions = [];
	const lineWidths = [];
	let previousLineTop = null;
	let lineStart = 0;
	for (let i = 0; i < layoutText.length; i++) {
		range.setStart(textNode, i);
		range.setEnd(textNode, i + 1);
		const top = range.getBoundingClientRect().top;
		if (previousLineTop !== null && top > previousLineTop + 1) {
			range.setStart(textNode, lineStart);
			range.setEnd(textNode, i);
			lineWidths.push(range.getBoundingClientRect().width);
			breakPositions.push(i);
			lineStart = i;
		}
		previousLineTop = top;
	}
	range.setStart(textNode, lineStart);
	range.setEnd(textNode, layoutText.length);
	lineWidths.push(range.getBoundingClientRect().width);
	return {
		breakPositions,
		lineWidths
	};
}
function refreshCipherContent_(cipherElement, text, lineBreakPositions, lineWidths) {
	cipherElement.textContent = "";
	const lineEnds = [...lineBreakPositions, text.length];
	let start = 0;
	for (let i = 0; i < lineEnds.length; i++) {
		const end = lineEnds[i];
		const lineSpan = document.createElement("span");
		lineSpan.style.cssText = `display:block;width:${lineWidths[i] ?? 0}px;white-space:nowrap;overflow:hidden`;
		let cipherLineText = "";
		for (let j = start; j < end; j++) cipherLineText += CIPHER_CHARS[Math.floor(Math.random() * 62)];
		lineSpan.textContent = cipherLineText;
		cipherElement.appendChild(lineSpan);
		start = end;
	}
}
function applyCipherLensMask_(cipherLayerElement, lensX, lensY) {
	const mask = `radial-gradient(circle ${CONFIG$1.lensRadius}px at ${lensX}px ${lensY}px, transparent ${CONFIG$1.lensRadius}px, black ${CONFIG$1.lensRadius}px)`;
	cipherLayerElement.style.setProperty("-webkit-mask-image", mask);
	cipherLayerElement.style.setProperty("mask-image", mask);
}
function startCipherAnimation_(cipherLayerElement, searchQueryText, lineBreakPositions, lineWidths) {
	cipherIntervalId_ = setInterval(() => {
		refreshCipherContent_(cipherLayerElement, searchQueryText, lineBreakPositions, lineWidths);
	}, CONFIG$1.cipherRefreshIntervalMs);
}
function stopCipherAnimation_() {
	if (cipherIntervalId_ !== null) {
		clearInterval(cipherIntervalId_);
		cipherIntervalId_ = null;
	}
}
function createSearchQueryLayerContainer_() {
	const element = document.createElement("span");
	element.className = "magnify-container";
	element.style.setProperty("--magnify-cipher-fade-duration", `${CONFIG$1.cipherFadeDurationMs}ms`);
	return element;
}
function createSearchQueryLayoutAnchorSpan_(searchQueryText) {
	const element = document.createElement("span");
	element.className = "magnify-layout";
	element.textContent = searchQueryText;
	return element;
}
function createSearchQueryCipherLayerSpan_() {
	const element = document.createElement("span");
	element.className = "magnify-cipher-layer";
	return element;
}
function createSearchQueryLayerSpan_(searchQueryText) {
	const element = document.createElement("span");
	element.className = "magnify-real-layer";
	element.style.color = autoTypeConfig.searchQueryTextColor;
	element.textContent = searchQueryText;
	return element;
}
function appendSearchQueryLayers_(searchQueryText) {
	const searchQueryLayerContainerElement = createSearchQueryLayerContainer_();
	searchQueryElement_$2.appendChild(searchQueryLayerContainerElement);
	const layoutSpanElement = createSearchQueryLayoutAnchorSpan_(searchQueryText);
	searchQueryLayerContainerElement.appendChild(layoutSpanElement);
	searchQueryCipherLayerSpanElement_ = createSearchQueryCipherLayerSpan_();
	searchQueryLayerContainerElement.appendChild(searchQueryCipherLayerSpanElement_);
	searchQueryLayerSpanElement_ = createSearchQueryLayerSpan_(searchQueryText);
	searchQueryLayerContainerElement.appendChild(searchQueryLayerSpanElement_);
	return searchQueryLayerContainerElement;
}
function applyMagnifyingGlassScanFrame_(scanX, searchQueryLayerContainerElement, searchQueryLayerContainerRect, searchQueryText, searchResultImageSrc) {
	if (!searchQueryCipherLayerSpanElement_ || !searchQueryLayerSpanElement_ || !lensElement_ || !lensBezelElement_) return;
	const lensCenterY = searchQueryLayerContainerRect.top + searchQueryLayerContainerElement.offsetHeight / 2 + CONFIG$1.lensYOffset;
	const cipherLeft = Math.max(0, Math.min(scanX + CONFIG$1.lensRadius, scanContainerWidth_));
	searchQueryCipherLayerSpanElement_.style.clipPath = `inset(0 0 0 ${cipherLeft}px)`;
	const lensCenterYInCipherLayer = lensCenterY - searchQueryLayerContainerRect.top;
	applyCipherLensMask_(searchQueryCipherLayerSpanElement_, scanX, lensCenterYInCipherLayer);
	const revealX = Math.max(0, Math.min(scanX - CONFIG$1.lensRadius, scanContainerWidth_));
	searchQueryLayerSpanElement_.style.clipPath = `inset(0 ${scanContainerWidth_ - revealX}px 0 0)`;
	const scanRange = scanContainerWidth_ + CONFIG$1.lensRadius * 2;
	const scanProgress = Math.max(0, Math.min(1, (scanX + CONFIG$1.lensRadius) / scanRange));
	const sourceCanvas = prepareLensSourceCanvas(contentElement_$2, searchQueryLayerContainerElement, searchQueryText, searchResultImageSrc);
	if (sourceCanvas) renderWebGLLens(sourceCanvas, scanContainerOffsetX_ + scanX, magnifyingGlassYInContent_, window.devicePixelRatio || 1, lensElement_.width, scanProgress);
	const lensPageX = searchQueryLayerContainerRect.left + scanX;
	const lensY = lensCenterY - CONFIG$1.lensRadius;
	const lensLeft = lensPageX - CONFIG$1.lensRadius;
	lensBezelElement_.style.left = `${lensLeft}px`;
	lensBezelElement_.style.top = `${lensY}px`;
	lensElement_.style.left = `${lensLeft}px`;
	lensElement_.style.top = `${lensY}px`;
}
function applyMagnifyingGlassOverlayCommonStyles_(element, lensY, diameter) {
	element.style.position = "fixed";
	element.style.top = `${lensY}px`;
	element.style.left = `-${diameter}px`;
	element.style.width = `${diameter}px`;
	element.style.height = `${diameter}px`;
	element.style.opacity = "0";
	element.style.transition = `opacity ${CONFIG$1.lensFadeDurationMs}ms ease`;
}
function createLensCanvas_(lensY, diameter) {
	const element = document.createElement("canvas");
	const physicalDiameter = Math.round(diameter * (window.devicePixelRatio || 1));
	element.width = physicalDiameter;
	element.height = physicalDiameter;
	applyMagnifyingGlassOverlayCommonStyles_(element, lensY, diameter);
	element.style.zIndex = "10001";
	element.style.transform = "none";
	element.style.pointerEvents = "none";
	element.style.clipPath = `circle(${CONFIG$1.lensRadius}px at ${CONFIG$1.lensRadius}px ${CONFIG$1.lensRadius}px)`;
	return element;
}
function createLensBezel_(lensY, diameter) {
	const element = document.createElement("span");
	element.className = "magnify-lens-bezel";
	applyMagnifyingGlassOverlayCommonStyles_(element, lensY, diameter);
	element.style.zIndex = "10002";
	element.style.transform = "none";
	return element;
}
function appendMagnifyingGlassOverlays_(lensY, diameter) {
	lensElement_ = createLensCanvas_(lensY, diameter);
	document.body.appendChild(lensElement_);
	lensBezelElement_ = createLensBezel_(lensY, diameter);
	document.body.appendChild(lensBezelElement_);
}
function initLensRenderer_() {
	if (!lensElement_) return false;
	const bodyBackgroundColor = window.getComputedStyle(document.body).backgroundColor;
	const [r, g, b] = bodyBackgroundColor === "rgba(0, 0, 0, 0)" || bodyBackgroundColor === "transparent" ? [
		0,
		0,
		0
	] : parseCssColor(bodyBackgroundColor);
	return initWebGLLens(lensElement_, [
		r / 255,
		g / 255,
		b / 255
	]);
}
function initMagnifyingGlass_(searchQueryLayerContainerElement) {
	const diameter = CONFIG$1.lensRadius * 2;
	appendMagnifyingGlassOverlays_(searchQueryLayerContainerElement.getBoundingClientRect().top + searchQueryLayerContainerElement.offsetHeight / 2 - CONFIG$1.lensRadius + CONFIG$1.lensYOffset, diameter);
	if (!initLensRenderer_()) {
		tearDownMagnifyingGlassOverlays_();
		return;
	}
	fadeInMagnifyingGlassOverlays_();
}
function fadeInMagnifyingGlassOverlays_() {
	requestAnimationFrame(() => {
		requestAnimationFrame(() => {
			lensElement_?.style.setProperty("opacity", "1");
			lensBezelElement_?.style.setProperty("opacity", "1");
		});
	});
}
function fadeOutMagnifyingGlassOverlays_() {
	lensElement_?.style.setProperty("opacity", "0");
	lensBezelElement_?.style.setProperty("opacity", "0");
}
function onMagnifyingGlassScanComplete_() {
	searchQueryLayerSpanElement_?.style.setProperty("clip-path", "");
	searchQueryCipherLayerSpanElement_?.classList.add("magnify-cipher-fade-out");
	fadeOutMagnifyingGlassOverlays_();
	stopCipherAnimation_();
	const fadeDuration = prefersReducedTransparency ? 0 : CONFIG$1.lensFadeDurationMs;
	magnifyingGlassFadeTimeoutId_ = setTimeout(() => {
		magnifyingGlassFadeTimeoutId_ = null;
		tearDownMagnifyingGlassOverlays_();
	}, fadeDuration);
}
function startMagnifyingGlassScan_(searchQueryLayerContainerElement, searchQueryText, searchResultImageSrc, onScanComplete) {
	scanContainerWidth_ = searchQueryLayerContainerElement.offsetWidth;
	const searchQueryLayerContainerRect = searchQueryLayerContainerElement.getBoundingClientRect();
	const contentRect = contentElement_$2.getBoundingClientRect();
	scanContainerOffsetX_ = searchQueryLayerContainerRect.left - contentRect.left;
	magnifyingGlassYInContent_ = searchQueryLayerContainerRect.top - contentRect.top + searchQueryLayerContainerElement.offsetHeight / 2 + CONFIG$1.lensYOffset;
	if (searchQueryLayerSpanElement_) searchQueryLayerSpanElement_.style.clipPath = `inset(0 ${scanContainerWidth_}px 0 0)`;
	let animationFrameId = null;
	let cancelled = false;
	const startX = -CONFIG$1.lensRadius;
	const endX = scanContainerWidth_ + CONFIG$1.lensRadius;
	const startTime = performance.now();
	const onTick = (now) => {
		if (cancelled) return;
		const searchQueryLayerContainerRect = searchQueryLayerContainerElement.getBoundingClientRect();
		const progress = Math.min((now - startTime) / CONFIG$1.scanDurationMs, 1);
		const easedProgress = progress < .5 ? 2 * progress * progress : -1 + (4 - 2 * progress) * progress;
		applyMagnifyingGlassScanFrame_(startX + (endX - startX) * easedProgress, searchQueryLayerContainerElement, searchQueryLayerContainerRect, searchQueryText, searchResultImageSrc);
		if (progress < 1) animationFrameId = requestAnimationFrame(onTick);
		else {
			cancelMagnifyingGlassScan_ = null;
			onScanComplete();
		}
	};
	animationFrameId = requestAnimationFrame(onTick);
	cancelMagnifyingGlassScan_ = () => {
		cancelled = true;
		if (animationFrameId !== null) {
			cancelAnimationFrame(animationFrameId);
			animationFrameId = null;
		}
		cancelMagnifyingGlassScan_ = null;
	};
}
function resetSearchQuery_$1() {
	if (!searchQueryElement_$2) return;
	searchQueryElement_$2.style.overflow = "";
	searchQueryElement_$2.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$2.textContent = "";
	searchQueryCipherLayerSpanElement_ = null;
	searchQueryLayerSpanElement_ = null;
}
function tearDownMagnifyingGlassOverlays_() {
	lensBezelElement_?.remove();
	lensBezelElement_ = null;
	lensElement_?.remove();
	lensElement_ = null;
	destroyWebGLLens();
}
function stopAnimationStart_() {
	if (startAnimationFrameId_ !== null) {
		cancelAnimationFrame(startAnimationFrameId_);
		startAnimationFrameId_ = null;
	}
}
function stopMagnifyingGlassFade_() {
	if (magnifyingGlassFadeTimeoutId_ !== null) {
		clearTimeout(magnifyingGlassFadeTimeoutId_);
		magnifyingGlassFadeTimeoutId_ = null;
	}
}
function stopAnimation_$2() {
	stopAnimationStart_();
	stopMagnifyingGlassFade_();
	cancelMagnifyingGlassScan_?.();
	stopCipherAnimation_();
	cancelAll$1();
	tearDownMagnifyingGlassOverlays_();
	resetSearchQuery_$1();
	simulateTap.stop();
}
function onSearchQueryComplete_$1(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$2);
	resetSearchQuery_$1();
	onComplete();
}
function animateSearchQuery_$1(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_$1();
	const searchQueryLayerContainerElement = appendSearchQueryLayers_(searchQueryText);
	startAnimationFrameId_ = requestAnimationFrame(() => {
		startAnimationFrameId_ = null;
		searchQueryElement_$2.style.overflow = "visible";
		const layoutElement = searchQueryLayerContainerElement.querySelector(".magnify-layout");
		const { breakPositions, lineWidths } = layoutElement ? measureLines_(layoutElement, searchQueryText) : {
			breakPositions: [],
			lineWidths: []
		};
		if (searchQueryCipherLayerSpanElement_) {
			refreshCipherContent_(searchQueryCipherLayerSpanElement_, searchQueryText, breakPositions, lineWidths);
			startCipherAnimation_(searchQueryCipherLayerSpanElement_, searchQueryText, breakPositions, lineWidths);
		}
		initMagnifyingGlass_(searchQueryLayerContainerElement);
		startMagnifyingGlassScan_(searchQueryLayerContainerElement, searchQueryText, searchResultImageSrc, onMagnifyingGlassScanComplete_);
	});
	scheduleAnimationComplete(CONFIG$1.scanDurationMs + CONFIG$1.cipherFadeDurationMs, scheduleAfter$1, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$2, contentElement_$2, searchResultImageSrc), () => onSearchQueryComplete_$1(onComplete));
}
/**
* Public.
*/
function init$7({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$2();
	searchQueryElement_$2 = searchQueryElement;
	searchResultImageElement_$2 = searchResultImageElement;
	contentElement_$2 = contentElement;
	simulateTap.init();
}
function simulate$2(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$2, scheduleAfter$1, () => animateSearchQuery_$1(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$2() {
	stopAnimation_$2();
}
var autoTypeMagnify = {
	init: init$7,
	simulate: simulate$2,
	cancel: cancel$2
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/redact/config.ts
var CONFIG = {
	characterStaggerMs: 28,
	blockOpacity: .52,
	blockFlickerDurationMs: 300,
	blockHoldMs: 60,
	revealDurationMs: 180
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/modes/redact/index.ts
/**
* Private.
*/
var searchQueryElement_$1;
var searchResultImageElement_$1;
var contentElement_$1;
var { scheduleAfter, cancelAll } = createScheduler();
function createCharacterSpan_(character) {
	const characterSpanElement = document.createElement("span");
	characterSpanElement.className = "redact-character";
	characterSpanElement.style.setProperty("--redact-block-opacity", String(CONFIG.blockOpacity));
	characterSpanElement.style.setProperty("--redact-flicker-duration", `${CONFIG.blockFlickerDurationMs}ms`);
	characterSpanElement.style.setProperty("--redact-reveal-duration", `${CONFIG.revealDurationMs}ms`);
	const layoutSpanElement = document.createElement("span");
	layoutSpanElement.className = "redact-layout";
	layoutSpanElement.textContent = character;
	characterSpanElement.appendChild(layoutSpanElement);
	const blockSpanElement = document.createElement("span");
	blockSpanElement.className = "redact-block";
	characterSpanElement.appendChild(blockSpanElement);
	const textSpanElement = document.createElement("span");
	textSpanElement.className = "redact-char-text";
	textSpanElement.textContent = character;
	characterSpanElement.appendChild(textSpanElement);
	return {
		characterSpanElement,
		blockSpanElement,
		textSpanElement
	};
}
function scheduleCharacterReveal_(blockSpanElement, textSpanElement, characterDelay) {
	scheduleAfter(() => {
		blockSpanElement.classList.add("redact-block-flicker");
	}, characterDelay);
	scheduleAfter(() => {
		blockSpanElement.style.animation = `redact-block-fade var(--redact-reveal-duration) ease-in forwards`;
		textSpanElement.classList.add("redact-char-reveal");
	}, characterDelay + CONFIG.blockFlickerDurationMs + CONFIG.blockHoldMs);
}
function animateCharacters_(searchQueryElement, searchQueryText) {
	searchQueryElement.textContent = "";
	searchQueryElement.style.overflow = "visible";
	const words = splitWords(searchQueryText);
	const wordSpanElements = createWordSpanElements(searchQueryElement, words);
	let characterIndex = 0;
	words.forEach((word, wordIndex) => {
		for (const character of word) {
			const { characterSpanElement, blockSpanElement, textSpanElement } = createCharacterSpan_(character);
			wordSpanElements[wordIndex].appendChild(characterSpanElement);
			scheduleCharacterReveal_(blockSpanElement, textSpanElement, characterIndex * CONFIG.characterStaggerMs);
			characterIndex++;
		}
		characterIndex++;
	});
}
function resetSearchQuery_() {
	if (!searchQueryElement_$1) return;
	searchQueryElement_$1.style.color = autoTypeConfig.searchQueryTextColor;
	searchQueryElement_$1.style.overflow = "";
	searchQueryElement_$1.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_$1.textContent = "";
}
function stopAnimation_$1() {
	cancelAll();
	resetSearchQuery_();
}
function onSearchQueryComplete_(onComplete) {
	simulateTap.stop();
	hideSearchResult$1(searchResultImageElement_$1);
	resetSearchQuery_();
	onComplete();
}
function animateSearchQuery_(searchQueryText, searchResultImageSrc, onComplete) {
	if (!searchQueryText.trim()) {
		onComplete();
		return;
	}
	simulateTap.start();
	resetSearchQuery_();
	animateCharacters_(searchQueryElement_$1, searchQueryText);
	scheduleAnimationComplete((searchQueryText.length - 1) * CONFIG.characterStaggerMs + CONFIG.blockFlickerDurationMs + CONFIG.blockHoldMs + CONFIG.revealDurationMs, scheduleAfter, () => simulateTap.startAnim(), () => showSearchResult(searchResultImageElement_$1, contentElement_$1, searchResultImageSrc), () => onSearchQueryComplete_(onComplete));
}
/**
* Public.
*/
function init$6({ searchQueryElement, searchResultImageElement, contentElement }) {
	stopAnimation_$1();
	searchQueryElement_$1 = searchQueryElement;
	searchResultImageElement_$1 = searchResultImageElement;
	contentElement_$1 = contentElement;
	simulateTap.init();
}
function simulate$1(searchQueryText, searchResultImageSrc, onComplete) {
	fadeOutPlaceholder(searchQueryElement_$1, scheduleAfter, () => animateSearchQuery_(searchQueryText, searchResultImageSrc, onComplete));
}
function cancel$1() {
	stopAnimation_$1();
}
var autoTypeRedact = {
	init: init$6,
	simulate: simulate$1,
	cancel: cancel$1
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/auto-type-random.ts
var autoTypeModes_ = [];
var autoTypeModeQueue_ = [];
var activeAutotypeMode_ = null;
var autoTypeModeSearchModeMap_ = /* @__PURE__ */ new Map();
function createCaretMode_() {
	let elements;
	return {
		init(newElements) {
			elements = newElements;
		},
		prepare() {
			autoTypeCaret.init(elements);
			autoTypeCaret.prepare?.();
		},
		simulate(searchQueryText, searchResultImageSrc, onComplete) {
			autoTypeCaret.simulate(searchQueryText, searchResultImageSrc, onComplete);
		},
		cancel() {
			autoTypeCaret.cancel();
		}
	};
}
/**
* Public.
*/
function init$5(elements, searchModes) {
	autoTypeModes_ = [];
	autoTypeModeQueue_ = [];
	autoTypeModeSearchModeMap_.clear();
	const caretMode = createCaretMode_();
	for (const [autoTypeMode, searchMode] of [
		[autoTypeAssemble, SearchMode.AutoTypeAssemble],
		[autoTypeBounce, SearchMode.AutoTypeBounce],
		[caretMode, SearchMode.AutoTypeCaret],
		[autoTypeFade, SearchMode.AutoTypeFade],
		[autoTypeFadeChars, SearchMode.AutoTypeFadeChars],
		[autoTypeFocus, SearchMode.AutoTypeFocus],
		[autoTypeGhost, SearchMode.AutoTypeGhost],
		[autoTypeMagnify, SearchMode.AutoTypeMagnify],
		[autoTypeNeon, SearchMode.AutoTypeNeon],
		[autoTypeRedact, SearchMode.AutoTypeRedact],
		[autoTypeReveal, SearchMode.AutoTypeReveal],
		[autoTypeScramble, SearchMode.AutoTypeScramble],
		[autoTypeSlotMachine, SearchMode.AutoTypeSlotMachine],
		[autoTypeSprinkle, SearchMode.AutoTypeSprinkle],
		[autoTypeWaterfall, SearchMode.AutoTypeWaterfall],
		[autoTypeWordBurst, SearchMode.AutoTypeWordBurst]
	]) {
		if (searchModes && !searchModes.includes(searchMode)) continue;
		autoTypeMode.init(elements);
		autoTypeModes_.push(autoTypeMode);
		autoTypeModeSearchModeMap_.set(autoTypeMode, searchMode);
	}
	debugLog(`AutoType random initialized with ${autoTypeModes_.length} animation ${autoTypeModes_.length === 1 ? "mode" : "modes"}`);
	if (autoTypeModes_.length === 0 && searchModes) {
		console.warn("No modes matched searchModes filter; falling back to all modes.");
		init$5(elements);
	}
}
function refillQueue_() {
	const shuffled = shuffle(autoTypeModes_);
	avoidRepeatAtStart(shuffled, activeAutotypeMode_);
	autoTypeModeQueue_.push(...shuffled);
}
function prepare() {
	if (autoTypeModeQueue_.length === 0) refillQueue_();
	const nextAutotypeMode = autoTypeModeQueue_.shift();
	if (nextAutotypeMode === void 0) {
		console.warn("prepare called with no animation modes available.");
		return;
	}
	activeAutotypeMode_ = nextAutotypeMode;
	debugLog(`AutoType random mode: ${getActiveModeName()}`);
	activeAutotypeMode_.prepare?.();
}
function simulate(searchQueryText, searchResultImageSrc, onComplete) {
	if (!activeAutotypeMode_) {
		console.warn("simulate called before prepare in autoTypeRandom.");
		onComplete();
		return;
	}
	activeAutotypeMode_.simulate(searchQueryText, searchResultImageSrc, onComplete);
}
function cancel() {
	activeAutotypeMode_?.cancel();
}
function getActiveModeName() {
	return (activeAutotypeMode_ && autoTypeModeSearchModeMap_.get(activeAutotypeMode_)) ?? SearchMode.AutoTypeRandom;
}
var autoTypeRandom = {
	init: init$5,
	prepare,
	simulate,
	cancel,
	getActiveModeName
};
//#endregion
//#region ../../house/brave_search/shared/src/auto-type/auto-type.ts
var searchQueryElement_;
var searchInputElement_$2;
var searchResultImageElement_;
var contentElement_;
var searchQueries_ = [];
var placeholder_ = "";
var activeAnimation_ = null;
var searchMode_ = SearchMode.AutoTypeFadeChars;
var searchBoxElement_$1 = null;
var searchBoxObserver_ = null;
var searchBoxAnimationFrameId_ = null;
var searchBoxTargetHeight_ = 0;
var currentSearchQuery_ = { query: "" };
var queryQueue_ = [];
var lastQueryIndex_ = -1;
var randomizeQueries_ = false;
var playAnimationFrameId_ = null;
var isActive_ = false;
var isFirstRun_ = true;
var isInitialized_ = false;
function resetToPlaceholder_(animate = true) {
	searchQueryElement_.style.opacity = "0";
	searchQueryElement_.classList.remove("search-query-fade-in", "search-query-fade-out");
	searchQueryElement_.textContent = placeholder_;
	searchQueryElement_.style.color = autoTypeConfig.placeholderColor;
	searchResultImageElement_.classList.remove("visible");
	contentElement_.classList.remove("content-search-result-shown");
	if (isFirstRun_ || !animate) {
		if (animate) isFirstRun_ = false;
		searchQueryElement_.style.opacity = "";
		return;
	}
	isFirstRun_ = false;
	searchQueryElement_.offsetWidth;
	searchQueryElement_.style.setProperty("--fade-in-duration", `${autoTypeConfig.placeholderFadeInDurationMs}ms`);
	searchQueryElement_.classList.add("search-query-fade-in");
	searchQueryElement_.style.opacity = "";
}
function nextQueryIndex_() {
	if (!randomizeQueries_) {
		const index = (lastQueryIndex_ + 1) % searchQueries_.length;
		lastQueryIndex_ = index;
		return index;
	}
	if (queryQueue_.length === 0) {
		const shuffled = shuffledIndices(searchQueries_.length);
		avoidRepeatAtStart(shuffled, lastQueryIndex_);
		queryQueue_.push(...shuffled);
	}
	const index = queryQueue_.shift();
	if (index === void 0) {
		console.warn("Query queue empty after refill; restarting from index 0.");
		return 0;
	}
	lastQueryIndex_ = index;
	return index;
}
function playNextQuery_() {
	if (!isActive_) return;
	if (!activeAnimation_) {
		console.warn("playNextQuery called with no active animation.");
		return;
	}
	if (searchQueries_.length === 0) {
		console.warn("playNextQuery called with no queries.");
		return;
	}
	resetToPlaceholder_();
	activeAnimation_.prepare?.();
	const searchModeName = searchMode_ === SearchMode.AutoTypeRandom ? autoTypeRandom.getActiveModeName() : searchMode_;
	document.dispatchEvent(new CustomEvent("autotype-mode-change", { detail: searchModeName }));
	const index = nextQueryIndex_();
	const searchQuery = searchQueries_[index];
	currentSearchQuery_ = searchQuery;
	activeAnimation_.simulate(searchQuery.query, searchQuery.image, () => {
		if (isActive_) contentElement_.classList.add("content-search-result-shown");
		playNextQuery_();
	});
}
function measureNaturalHeight_(searchBoxElement) {
	const savedTransition = searchBoxElement.style.transition;
	searchBoxElement.style.transition = "none";
	searchBoxElement.style.height = "";
	return {
		naturalHeight: searchBoxElement.offsetHeight,
		savedTransition
	};
}
function animateToNaturalHeight_(searchBoxElement, visualHeight, savedTransition) {
	searchBoxElement.style.height = `${visualHeight}px`;
	searchBoxAnimationFrameId_ = requestAnimationFrame(() => {
		searchBoxAnimationFrameId_ = null;
		const shouldAnimate = contentElement_.classList.contains("search-result-visible");
		searchBoxElement.style.transition = shouldAnimate ? "height 0.2s ease" : "none";
		searchBoxElement.style.height = `${searchBoxTargetHeight_}px`;
		if (shouldAnimate) searchBoxElement.addEventListener("transitionend", () => {
			searchBoxElement.style.height = "";
			searchBoxElement.style.transition = "";
		}, { once: true });
		else {
			searchBoxElement.style.height = "";
			searchBoxElement.style.transition = savedTransition;
		}
	});
}
function onSearchBoxMutation_() {
	const searchBoxElement = searchBoxElement_$1;
	if (!searchBoxElement) return;
	const visualHeight = Math.round(searchBoxElement.getBoundingClientRect().height);
	const { naturalHeight, savedTransition } = measureNaturalHeight_(searchBoxElement);
	if (naturalHeight === searchBoxTargetHeight_) {
		searchBoxElement.style.height = "";
		searchBoxElement.style.transition = savedTransition;
		return;
	}
	searchBoxTargetHeight_ = naturalHeight;
	if (searchBoxAnimationFrameId_ !== null) {
		cancelAnimationFrame(searchBoxAnimationFrameId_);
		searchBoxAnimationFrameId_ = null;
	}
	animateToNaturalHeight_(searchBoxElement, visualHeight, savedTransition);
}
function watchSearchBoxSize_() {
	searchBoxElement_$1 = searchQueryElement_.closest(".search-box");
	if (!searchBoxElement_$1) {
		console.warn("Search box element not found; height animation disabled.");
		return;
	}
	searchBoxTargetHeight_ = searchBoxElement_$1.offsetHeight;
	if (searchBoxAnimationFrameId_ !== null) {
		cancelAnimationFrame(searchBoxAnimationFrameId_);
		searchBoxAnimationFrameId_ = null;
	}
	searchBoxObserver_?.disconnect();
	searchBoxObserver_ = new MutationObserver(onSearchBoxMutation_);
	searchBoxObserver_.observe(searchQueryElement_, {
		childList: true,
		subtree: true,
		characterData: true
	});
}
function stopAnimation_() {
	if (playAnimationFrameId_ !== null) {
		cancelAnimationFrame(playAnimationFrameId_);
		playAnimationFrameId_ = null;
	}
	activeAnimation_?.cancel();
}
function initContentStyles_() {
	contentElement_.style.setProperty("--content-slide-up-on-result-visible", `${autoTypeConfig.contentSlideUpOnResultVisible}px`);
	contentElement_.style.setProperty("--content-slide-up-duration", `${autoTypeConfig.contentSlideUpDurationMs}ms`);
	contentElement_.style.setProperty("--content-slide-up-easing", autoTypeConfig.contentSlideUpEasing);
}
function createSearchResultImageElement_() {
	const imgElement = document.createElement("img");
	imgElement.className = "search-result-image hidden";
	imgElement.style.cursor = "pointer";
	imgElement.style.setProperty("--search-result-image-border-radius", `${autoTypeConfig.searchResultImageBorderRadius}px`);
	imgElement.style.setProperty("--search-result-image-overlap-offset", `${autoTypeConfig.searchResultImageTopOffset}px`);
	imgElement.style.setProperty("--search-result-image-max-width", `${autoTypeConfig.searchResultImageMaxWidth}px`);
	imgElement.style.setProperty("--search-result-image-fade-in-duration", `${autoTypeConfig.searchResultImageFadeInDurationMs}ms`);
	imgElement.addEventListener("click", () => {
		if (currentSearchQuery_.query && imgElement.classList.contains("visible")) {
			dispatchRichMediaEvent(richMediaEventTypes.CLICK);
			searchDispatcher.dispatchSearchWithQuery(currentSearchQuery_);
		}
	});
	contentElement_.appendChild(imgElement);
	return imgElement;
}
function initAnimation_() {
	const animationElements = {
		searchQueryElement: searchQueryElement_,
		searchResultImageElement: searchResultImageElement_,
		contentElement: contentElement_
	};
	const standardMode = {
		[SearchMode.AutoTypeFadeChars]: autoTypeFadeChars,
		[SearchMode.AutoTypeFade]: autoTypeFade,
		[SearchMode.AutoTypeScramble]: autoTypeScramble,
		[SearchMode.AutoTypeWordBurst]: autoTypeWordBurst,
		[SearchMode.AutoTypeBounce]: autoTypeBounce,
		[SearchMode.AutoTypeReveal]: autoTypeReveal,
		[SearchMode.AutoTypeSlotMachine]: autoTypeSlotMachine,
		[SearchMode.AutoTypeFocus]: autoTypeFocus,
		[SearchMode.AutoTypeNeon]: autoTypeNeon,
		[SearchMode.AutoTypeGhost]: autoTypeGhost,
		[SearchMode.AutoTypeWaterfall]: autoTypeWaterfall,
		[SearchMode.AutoTypeAssemble]: autoTypeAssemble,
		[SearchMode.AutoTypeSprinkle]: autoTypeSprinkle,
		[SearchMode.AutoTypeReducedMotion]: autoTypeReducedMotion,
		[SearchMode.AutoTypeMagnify]: autoTypeMagnify,
		[SearchMode.AutoTypeRedact]: autoTypeRedact
	}[searchMode_];
	if (standardMode) {
		standardMode.init(animationElements);
		activeAnimation_ = standardMode;
	} else if (searchMode_ === SearchMode.AutoTypeRandom) {
		const searchModes = splitSearchModes(searchBoxElement_$1?.dataset.searchMode ?? "");
		autoTypeRandom.init(animationElements, searchModes.length > 1 ? searchModes : void 0);
		activeAnimation_ = autoTypeRandom;
	} else {
		autoTypeCaret.init(animationElements);
		activeAnimation_ = autoTypeCaret;
	}
}
/**
* Public.
*/
function init$4(queries, searchMode) {
	stopAnimation_();
	isInitialized_ = false;
	isActive_ = false;
	isFirstRun_ = true;
	const searchQueryElement = document.querySelector("#search-query");
	const searchInputElement = document.querySelector("#search-input");
	const contentElement = document.querySelector(".content");
	if (!searchQueryElement || !searchInputElement || !contentElement) {
		console.warn("Required elements not found, failed to initialize autotype.");
		return;
	}
	searchQueryElement_ = searchQueryElement;
	searchInputElement_$2 = searchInputElement;
	contentElement_ = contentElement;
	initContentStyles_();
	watchSearchBoxSize_();
	contentElement_.querySelector(".search-result-image")?.remove();
	searchResultImageElement_ = createSearchResultImageElement_();
	searchQueries_ = queries;
	placeholder_ = searchInputElement_$2.placeholder;
	randomizeQueries_ = searchBoxElement_$1?.dataset.randomizeQueries !== void 0 && queries.length > 1;
	debugLog(`Playing queries in ${randomizeQueries_ ? "random order" : "sequence"}`);
	queryQueue_ = [];
	lastQueryIndex_ = -1;
	searchMode_ = searchMode;
	initAnimation_();
	isInitialized_ = true;
}
function start() {
	if (!isInitialized_ || isActive_) return;
	debugLog(`AutoType started in ${searchMode_} mode`);
	isActive_ = true;
	searchResultImageElement_.classList.remove("hidden");
	playAnimationFrameId_ = requestAnimationFrame(() => {
		playNextQuery_();
	});
}
function pause() {
	if (!isActive_) return;
	debugLog("AutoType paused");
	isActive_ = false;
	stopAnimation_();
	isFirstRun_ = true;
	resetToPlaceholder_(false);
}
function resume() {
	if (!isInitialized_ || document.hidden) return;
	if (!searchInputElement_$2.classList.contains("hidden")) return;
	debugLog("AutoType resumed");
	start();
}
function getSearchQuery$1() {
	return currentSearchQuery_;
}
function hideSearchResult() {
	if (!isInitialized_) return;
	searchResultImageElement_.classList.remove("visible");
	searchResultImageElement_.classList.add("hidden");
}
var autoType = {
	init: init$4,
	start,
	pause,
	resume,
	getSearchQuery: getSearchQuery$1,
	hideSearchResult
};
//#endregion
//#region ../../house/brave_search/shared/src/search/autocomplete-ask-brave-row.ts
/**
* Public.
*/
function createElement$1(onClick) {
	if (document.querySelector(".search-box")?.dataset.hideAskBrave !== void 0) return null;
	const iconElement = document.createElement("span");
	iconElement.className = "ask-brave-icon";
	const labelElement = document.createElement("span");
	labelElement.className = "ask-brave-label";
	labelElement.textContent = "Ask Brave";
	const rowElement = document.createElement("div");
	rowElement.className = "ask-brave suggestion";
	rowElement.append(iconElement, labelElement);
	rowElement.addEventListener("click", onClick);
	return rowElement;
}
var autocompleteAskBraveRow = { createElement: createElement$1 };
//#endregion
//#region ../../house/brave_search/shared/src/search/autocomplete-suggestion-row.ts
/**
* Public.
*/
function createElement(contents, descriptionText, onClick) {
	const titleElement = document.createElement("div");
	titleElement.className = "suggestion-title";
	titleElement.textContent = contents;
	const imageElement = document.createElement("span");
	imageElement.className = "suggestion-image";
	const descriptionElement = document.createElement("div");
	descriptionElement.className = "suggestion-description";
	descriptionElement.textContent = descriptionText;
	const textContainerElement = document.createElement("div");
	textContainerElement.className = "suggestion-text-container";
	textContainerElement.append(titleElement, descriptionElement);
	const rowElement = document.createElement("div");
	rowElement.className = "suggestion";
	rowElement.append(imageElement, textContainerElement);
	rowElement.addEventListener("click", onClick);
	return rowElement;
}
var autocompleteSuggestionRow = { createElement };
//#endregion
//#region ../../house/brave_search/shared/src/search/autocomplete.config.ts
var autocompleteConfig = { maxSuggestions: 5 };
//#endregion
//#region ../../house/brave_search/shared/src/search/autocomplete.ts
/**
* Private.
*/
var ASK_LEO_DESCRIPTION_ = "Ask Leo";
var searchContainerElement_;
var searchInputElement_$1;
var onSuggestionClick_;
var onAskBraveClick_;
var isSubscribed_ = false;
var activeIndex_ = -1;
function handleMessage_(event) {
	if (!isTrustedOrigin(event.origin)) return;
	const { type, value: suggestions } = event.data || {};
	if (type === "richMediaSearchMatches" && Array.isArray(suggestions)) update_(suggestions);
}
function createRowElement_(contents, description) {
	if (description !== ASK_LEO_DESCRIPTION_) return autocompleteSuggestionRow.createElement(contents, description, () => onSuggestionClick_(contents));
	return autocompleteAskBraveRow.createElement(() => {
		const query = searchInputElement_$1.value.trim();
		if (!query) return;
		onAskBraveClick_(query);
	});
}
function update_(suggestions) {
	hide();
	if (suggestions.length === 0) return;
	if (!searchInputElement_$1?.value.trim()) return;
	const suggestionsContainerElement = document.createElement("div");
	suggestionsContainerElement.id = "suggestions";
	suggestionsContainerElement.className = "suggestions";
	for (const { contents, description } of suggestions.slice(0, autocompleteConfig.maxSuggestions)) {
		const rowElement = createRowElement_(contents, description);
		if (rowElement) suggestionsContainerElement.appendChild(rowElement);
	}
	if (suggestionsContainerElement.children.length === 0) return;
	searchContainerElement_.appendChild(suggestionsContainerElement);
}
/**
* Public.
*/
function init$3(searchContainerElement, searchInputElement, onSuggestionClick, onAskBraveClick) {
	searchContainerElement_ = searchContainerElement;
	searchInputElement_$1 = searchInputElement;
	onSuggestionClick_ = onSuggestionClick;
	onAskBraveClick_ = onAskBraveClick;
}
function subscribe() {
	if (!isSubscribed_) {
		window.addEventListener("message", handleMessage_);
		isSubscribed_ = true;
	}
}
function hide() {
	activeIndex_ = -1;
	document.getElementById("suggestions")?.remove();
}
function navigate(direction) {
	const suggestionsContainerElement = document.getElementById("suggestions");
	if (!suggestionsContainerElement) return null;
	const itemElements = suggestionsContainerElement.querySelectorAll(".suggestion");
	if (itemElements.length === 0) return null;
	itemElements[activeIndex_]?.classList.remove("suggestion--active");
	activeIndex_ += direction;
	if (activeIndex_ >= itemElements.length) activeIndex_ = -1;
	if (activeIndex_ < -1) activeIndex_ = itemElements.length - 1;
	if (activeIndex_ === -1) return null;
	itemElements[activeIndex_].classList.add("suggestion--active");
	return itemElements[activeIndex_].querySelector(".suggestion-title")?.textContent ?? null;
}
function getActive() {
	if (activeIndex_ === -1) return null;
	const suggestionsContainerElement = document.getElementById("suggestions");
	if (!suggestionsContainerElement) return null;
	return suggestionsContainerElement.querySelectorAll(".suggestion")[activeIndex_]?.querySelector(".suggestion-title")?.textContent ?? null;
}
function getActiveElement() {
	if (activeIndex_ === -1) return null;
	return document.getElementById("suggestions")?.querySelectorAll(".suggestion")[activeIndex_] ?? null;
}
function reset() {
	const suggestionsContainerElement = document.getElementById("suggestions");
	if (suggestionsContainerElement) suggestionsContainerElement.querySelectorAll(".suggestion--active").forEach((activeItemElement) => activeItemElement.classList.remove("suggestion--active"));
	activeIndex_ = -1;
}
var autocomplete = {
	init: init$3,
	subscribe,
	hide,
	navigate,
	getActive,
	getActiveElement,
	reset
};
//#endregion
//#region ../../house/brave_search/shared/src/search/search-box.ts
/**
* Private.
*/
var searchInputElement_;
var autoTypeQueryElement_;
var searchBoxElement_;
var searchBoxLogoElement_ = null;
var searchBoxIconButtonElement_ = null;
function getSearchQuery_() {
	return (searchInputElement_.classList.contains("hidden") ? autoType.getSearchQuery().query : searchInputElement_.value).trim();
}
function addSearchLogo_() {
	searchBoxLogoElement_ = document.createElement("img");
	searchBoxLogoElement_.className = "search-logo";
	searchBoxLogoElement_.src = "search-logo.webp";
	searchBoxElement_.prepend(searchBoxLogoElement_);
}
function addSearchIconButton_() {
	searchBoxIconButtonElement_ = document.createElement("button");
	searchBoxIconButtonElement_.className = "search-icon-button";
	searchBoxElement_.append(searchBoxIconButtonElement_);
	searchBoxIconButtonElement_.addEventListener("click", (event) => {
		event.stopPropagation();
		const searchQuery = getSearchQuery_();
		if (searchQuery) {
			dispatchRichMediaEvent(richMediaEventTypes.CLICK);
			searchDispatcher.dispatchSearchWithQuery({
				query: searchQuery,
				params: getSearchQueryParams()
			});
		}
	});
}
function switchToInteractiveTypingMode_(placeholder) {
	debugLog("Switched to interactive typing mode");
	autoType.pause();
	autoType.hideSearchResult();
	autoTypeQueryElement_.classList.add("hidden");
	searchInputElement_.classList.remove("hidden");
	searchInputElement_.placeholder = placeholder;
	searchInputElement_.focus();
	autocomplete.subscribe();
	dispatchRichMediaEvent(richMediaEventTypes.INTERACTION);
}
function setupInput_() {
	let originalQuery = "";
	searchInputElement_.addEventListener("click", (event) => {
		event.stopPropagation();
		dispatchRichMediaEvent(richMediaEventTypes.INTERACTION);
	});
	searchInputElement_.addEventListener("keydown", (event) => {
		if (event.key === "ArrowDown" || event.key === "ArrowUp") {
			event.preventDefault();
			const selected = autocomplete.navigate(event.key === "ArrowDown" ? 1 : -1);
			searchInputElement_.value = selected ?? originalQuery;
		} else if (event.key === "Enter") {
			const activeElement = autocomplete.getActiveElement();
			if (activeElement) activeElement.click();
			else {
				const query = searchInputElement_.value.trim();
				if (query) {
					dispatchRichMediaEvent(richMediaEventTypes.CLICK);
					searchDispatcher.dispatchSearchWithQuery({ query });
				}
			}
		} else if (event.key === "Escape") if (autocomplete.getActiveElement() !== null) {
			searchInputElement_.value = originalQuery;
			autocomplete.reset();
		} else {
			searchInputElement_.value = "";
			searchInputElement_.blur();
			autocomplete.hide();
		}
	});
	searchInputElement_.addEventListener("input", () => {
		originalQuery = searchInputElement_.value;
		if (searchInputElement_.value.trim()) searchDispatcher.dispatchQueryAutocomplete(searchInputElement_.value);
		else autocomplete.hide();
	});
}
/**
* Public.
*/
function init$2(searchMode) {
	const searchContainerElement = document.querySelector(".search-container");
	const searchBoxElement = document.querySelector(".search-box");
	if (!searchContainerElement || !searchBoxElement) {
		console.warn("Required elements not found, failed to initialize search box.");
		return;
	}
	searchBoxElement_ = searchBoxElement;
	const placeholder = localizeText(searchBoxElement_.dataset.placeholder, "Ask anything, find anything...");
	const searchInputElement = document.createElement("input");
	searchInputElement.id = "search-input";
	searchInputElement.type = "text";
	searchInputElement.autocomplete = "off";
	searchInputElement.spellcheck = false;
	searchInputElement.setAttribute("autocorrect", "off");
	searchInputElement.setAttribute("autocapitalize", "off");
	searchInputElement.placeholder = placeholder;
	searchBoxElement_.append(searchInputElement);
	searchInputElement_ = searchInputElement;
	if (searchBoxElement_.dataset.hideSearchLogo === void 0) addSearchLogo_();
	if (searchBoxElement_.dataset.hideSearchIcon === void 0) addSearchIconButton_();
	if (searchMode === SearchMode.Interactive) {
		searchBoxElement_.addEventListener("click", (event) => {
			event.stopPropagation();
		});
		setupInput_();
		autocomplete.init(searchContainerElement, searchInputElement, (query) => {
			dispatchRichMediaEvent(richMediaEventTypes.CLICK);
			searchDispatcher.dispatchSearchWithQuery({ query });
		}, (query) => {
			dispatchRichMediaEvent(richMediaEventTypes.CLICK);
			searchDispatcher.dispatchAskBrave(query);
		});
		autocomplete.subscribe();
		return;
	}
	const autoTypeQueryElement = document.createElement("div");
	autoTypeQueryElement.id = "search-query";
	searchBoxElement_.insertBefore(autoTypeQueryElement, searchInputElement);
	searchInputElement.classList.add("hidden");
	autoTypeQueryElement_ = autoTypeQueryElement;
	autocomplete.init(searchContainerElement, searchInputElement, (query) => {
		dispatchRichMediaEvent(richMediaEventTypes.CLICK);
		searchDispatcher.dispatchSearchWithQuery({ query });
	}, (query) => {
		dispatchRichMediaEvent(richMediaEventTypes.CLICK);
		searchDispatcher.dispatchAskBrave(query);
	});
	searchBoxElement_.addEventListener("click", (event) => {
		event.stopPropagation();
		if (!searchInputElement.classList.contains("hidden")) return;
		switchToInteractiveTypingMode_(placeholder);
	});
	searchBoxElement_.addEventListener("mouseenter", () => {
		if (!searchInputElement.classList.contains("hidden")) return;
		if (document.hidden) return;
		autoType.pause();
	});
	searchBoxElement_.addEventListener("mouseleave", () => {
		if (!searchInputElement.classList.contains("hidden")) return;
		if (document.hidden) return;
		autoType.resume();
	});
	setupInput_();
}
function getSearchBoxLogoElement() {
	return searchBoxLogoElement_;
}
function getSearchBoxIconButtonElement() {
	return searchBoxIconButtonElement_;
}
function getSearchQuery() {
	return getSearchQuery_();
}
function getSearchQueryParams() {
	return searchInputElement_.classList.contains("hidden") ? autoType.getSearchQuery().params : void 0;
}
var searchBox = {
	init: init$2,
	getSearchBoxLogoElement,
	getSearchBoxIconButtonElement,
	getSearchQuery,
	getSearchQueryParams
};
//#endregion
//#region ../../house/brave_search/shared/src/buttons/try-now-button.config.ts
var tryNowButtonConfig = {
	backgroundColor: "#ff4000",
	textColor: "white",
	animCycles: 3,
	hoverScale: 1.05
};
//#endregion
//#region ../../house/brave_search/shared/src/buttons/try-now-button.ts
/**
* Public.
*/
function init$1(getSearchQuery, getSearchQueryParams) {
	const buttonElement = document.getElementById("try-now-button");
	if (!buttonElement) return;
	buttonElement.textContent = localizeText(buttonElement.dataset.label, "Try Brave Search Now");
	buttonElement.style.setProperty("--try-now-button-background-color", buttonElement.dataset.backgroundColor ?? tryNowButtonConfig.backgroundColor);
	buttonElement.style.setProperty("--try-now-button-color", buttonElement.dataset.textColor ?? tryNowButtonConfig.textColor);
	buttonElement.style.setProperty("--anim-cycles", `${tryNowButtonConfig.animCycles}`);
	buttonElement.style.setProperty("--try-now-button-hover-scale", `${tryNowButtonConfig.hoverScale}`);
	if (buttonElement.dataset.hoverBackgroundColor) {
		buttonElement.style.setProperty("--try-now-button-hover-background-color", buttonElement.dataset.hoverBackgroundColor);
		buttonElement.style.setProperty("--try-now-button-hover-filter", "none");
	}
	buttonElement.addEventListener("click", (event) => {
		event.stopPropagation();
		dispatchRichMediaEvent(richMediaEventTypes.CLICK);
		searchDispatcher.dispatchSearchWithQuery({
			query: getSearchQuery(),
			params: getSearchQueryParams()
		});
	});
}
var tryNowButton = { init: init$1 };
//#endregion
//#region ../../house/brave_search/shared/src/buttons/make-default-button.config.ts
var makeDefaultButtonConfig = {
	backgroundColor: "#303033",
	textColor: "white"
};
//#endregion
//#region ../../house/brave_search/shared/src/buttons/make-default-button.ts
/**
* Public.
*/
function init() {
	const buttonElement = document.getElementById("make-default-button");
	if (!buttonElement) return;
	buttonElement.textContent = localizeText(buttonElement.dataset.label, "Make Brave Search Default");
	buttonElement.style.setProperty("--make-default-button-background-color", buttonElement.dataset.backgroundColor ?? makeDefaultButtonConfig.backgroundColor);
	buttonElement.style.setProperty("--make-default-button-color", buttonElement.dataset.textColor ?? makeDefaultButtonConfig.textColor);
	if (buttonElement.dataset.hoverBackgroundColor) {
		buttonElement.style.setProperty("--make-default-button-hover-background-color", buttonElement.dataset.hoverBackgroundColor);
		buttonElement.style.setProperty("--make-default-button-hover-filter", "none");
	}
	buttonElement.addEventListener("click", (event) => {
		event.stopPropagation();
		searchDispatcher.dispatchMakeDefault();
		dispatchRichMediaEvent(richMediaEventTypes.INTERACTION);
	});
}
var makeDefaultButton = { init };
//#endregion
//#region ../../house/brave_search/shared/src/brave-search-init.ts
/**
* Private.
*/
var visibilityListenerRegistered_ = false;
function resolveSearchMode_(searchModes) {
	const searchMode = searchModes.length > 1 ? SearchMode.AutoTypeRandom : searchModes[0] ?? SearchMode.AutoTypeFadeChars;
	if (prefersReducedMotion) return SearchMode.AutoTypeReducedMotion;
	return searchMode;
}
function initTagline_() {
	const taglineElement = document.getElementById("tagline");
	if (!taglineElement) return;
	const taglineText = localizeText(taglineElement.dataset.label, "");
	if (taglineText) taglineElement.textContent = taglineText;
	else taglineElement.style.display = "none";
}
function initSubHeadline_() {
	const subHeadlineElement = document.getElementById("sub-headline");
	if (!subHeadlineElement) return;
	const subHeadlineText = localizeText(subHeadlineElement.dataset.label, "");
	if (subHeadlineText) subHeadlineElement.textContent = subHeadlineText;
	else subHeadlineElement.style.visibility = "hidden";
}
function initTryNowButton_(searchBoxElement) {
	if (searchBoxElement.dataset.hideTryNowButton !== void 0) {
		const tryNowButtonElement = document.getElementById("try-now-button");
		if (tryNowButtonElement) tryNowButtonElement.style.display = "none";
		return;
	}
	tryNowButton.init(() => searchBox.getSearchQuery(), () => searchBox.getSearchQueryParams());
}
function initAutoType_(searchQueries, searchMode) {
	autoType.init(searchQueries, searchMode);
	autoType.start();
	if (!visibilityListenerRegistered_) {
		visibilityListenerRegistered_ = true;
		document.addEventListener("visibilitychange", () => {
			debugLog(`Visibility changed: ${document.hidden ? "hidden" : "visible"}`);
			if (document.hidden) autoType.pause();
			else autoType.resume();
		});
	}
}
function initMakeDefaultButton_(searchBoxElement) {
	const makeDefaultButtonElement = document.getElementById("make-default-button");
	if (searchBoxElement.dataset.hideMakeDefaultButton !== void 0 || !isMajorBrowserVersionAtLeast(148)) {
		if (makeDefaultButtonElement) makeDefaultButtonElement.style.display = "none";
		return;
	}
	makeDefaultButton.init();
}
/**
* Public.
*/
function initBraveSearch() {
	document.addEventListener("DOMContentLoaded", () => {
		initSafeArea();
		initWallpaper();
		if (!document.querySelector(".search-container")) {
			console.warn("Search container not found, failed to initialize.");
			return;
		}
		const searchBoxElement = document.querySelector(".search-box");
		if (!searchBoxElement) {
			console.warn("Search box not found, failed to initialize.");
			return;
		}
		if (searchBoxElement.dataset.hideNtpSearchBox !== void 0) searchDispatcher.dispatchHideBraveSearchBox();
		const json = searchBoxElement.dataset.searchQueries;
		let searchQueryLocaleMap;
		if (json) try {
			searchQueryLocaleMap = JSON.parse(json);
		} catch {
			console.warn("data-search-queries is not valid JSON; autotype will be disabled.");
		}
		const searchQueries = localizeContent(searchQueryLocaleMap);
		const resolvedSearchMode = resolveSearchMode_(splitSearchModes(searchBoxElement.dataset.searchMode ?? ""));
		const searchMode = searchQueries.length === 0 ? SearchMode.Interactive : resolvedSearchMode;
		debugLog(`Navigator languages: ${[...navigator.languages].join(", ")}`);
		debugLog(`Primary navigator locale: ${locale.tag}`);
		debugLog(`Primary navigator language: ${locale.language}`);
		debugLog(`Primary navigator region: ${locale.region ?? "unknown"}`);
		searchBox.init(searchMode);
		initTagline_();
		initSubHeadline_();
		initTryNowButton_(searchBoxElement);
		initMakeDefaultButton_(searchBoxElement);
		if (searchMode !== SearchMode.Interactive) initAutoType_(searchQueries, searchMode);
	});
}
//#endregion
//#region src/brave-search.ts
initBraveSearch();
//#endregion
