<script setup lang="ts">
import { computed } from 'vue'
import { Select } from '@atlanhq/atlantis'
import type { SelectWidget } from '~/types/workflows'

const props = defineProps<{
    property: SelectWidget & { id: string }
    disabled?: boolean
    options?: Array<{ label: string; value: string }>
}>()

const modelValue = defineModel<string>()

const selectOptions = computed(() => {
    return props.options || []
})
</script> 

<template>
        <Select
            :id="property.id"
            v-model="modelValue"
            :options="selectOptions"
            :placeholder="property.placeholder"
            :is-disabled="disabled"
            variant="default"
        />
</template>

