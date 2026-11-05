/**
 * Marks a branch the type system has proven unreachable.
 *
 * Use it as the `default` of a `switch` over a union (a state, an event, a
 * verdict): adding a member to the union then fails to compile wherever a case
 * is missing, and a value that slips past the types at runtime still throws
 * instead of falling through silently.
 */
export function assertNever(value: never, what = "value"): never {
  throw new Error(`Unexpected ${what}: ${JSON.stringify(value)}`);
}
