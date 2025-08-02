<script setup lang="ts">
    import { Input } from '@atlanhq/atlantis'
    import type { InputWidget } from '~/types/workflows'

    const { property, disabled, field } = defineProps<{
        property: InputWidget & { ui: Record<string, unknown> }
        disabled?: boolean
        field: unknown
    }>()

    const componentProps = computed(() => property.ui)
</script>

<template>
    <Input
        :modelValue="field.state.value"
        v-bind="componentProps"
        :disabled="property.BYOCdisabled || disabled"
        variant="default"
        state="idle"
        @update:modelValue="field.handleChange"
        @blur="field.handleBlur"
    />
</template>
