<script setup lang="ts">
    import { Input } from '@atlanhq/atlantis'
    import type { PasswordWidget } from '~/types/workflows'

    const { property, field } = defineProps<{
        property: PasswordWidget & { id: string; ui: Record<string, unknown> }
        disabled?: boolean
        field: unknown
    }>()

    const componentProps = computed(() => property.ui)
</script>

<template>
    <Input
        :id="property.id"
        :modelValue="field.state.value"
        variant="default"
        state="idle"
        v-bind="componentProps"
        :disabled="disabled"
        type="password"
        @update:modelValue="field.handleChange"
        @blur="field.handleBlur"
    />
</template>
