import { describe, expect, expectTypeOf, it } from "vitest";

import { assertNever, type Brand } from "./index.js";

type Light = "red" | "green";

function next(light: Light): Light {
  switch (light) {
    case "red":
      return "green";
    case "green":
      return "red";
    default:
      return assertNever(light, "light");
  }
}

describe("assertNever", () => {
  it("is never reached for a value of the union", () => {
    expect(next("red")).toBe("green");
    expect(next("green")).toBe("red");
  });

  it("throws, naming the value, when a value slips past the types", () => {
    expect(() => next("amber" as Light)).toThrow('Unexpected light: "amber"');
  });
});

describe("Brand", () => {
  it("keeps identifiers of different entities apart", () => {
    type UserId = Brand<string, "UserId">;
    type DateId = Brand<string, "DateId">;
    expectTypeOf<UserId>().not.toEqualTypeOf<DateId>();
    expectTypeOf<UserId>().toExtend<string>();
  });
});
