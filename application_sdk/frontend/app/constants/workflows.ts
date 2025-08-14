import type { InjectionKey, Ref } from 'vue'
import type { useForm } from '@tanstack/vue-form'

export const FormInjectionKey = Symbol('form') as InjectionKey<
    ReturnType<typeof useForm>
>
