<script setup lang="ts">
    import { computed } from 'vue'
    import { Select } from '@atlanhq/atlantis'
    import type { SelectWidget } from '~/types/workflows'

    const { options, field } = defineProps<{
        property: SelectWidget & { id: string }
        disabled?: boolean
        options?: Array<{ label: string; value: string }>
        field: unknown
    }>()

    const selectOptions = computed(() => options || [])
</script>

<template>
    <Select
        :id="property.id"
        :modelValue="field.state.value"
        :options="selectOptions"
        :placeholder="property.placeholder"
        :is-disabled="disabled"
        variant="default"
        @change="field.handleChange"
        @blur="field.handleBlur"
    />
</template>
