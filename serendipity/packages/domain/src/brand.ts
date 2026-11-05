declare const brand: unique symbol;

/**
 * A nominal type: `Brand<string, "UserId">` is a string that the compiler will
 * not accept where a `Brand<string, "DateId">` is expected. Identifiers of
 * different entities can then never be swapped by accident.
 */
export type Brand<T, Name extends string> = T & { readonly [brand]: Name };
