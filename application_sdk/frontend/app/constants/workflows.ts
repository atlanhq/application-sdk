import type { InjectionKey, Ref } from 'vue'

export const FormStateInjectionKey = Symbol('formState') as InjectionKey<
    Ref<Record<string, unknown>>
>
