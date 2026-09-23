import tseslint from "typescript-eslint";

export default [{
  files: ["src/**/*.{ts,tsx}"],
  languageOptions: { parser: tseslint.parser },
}];
